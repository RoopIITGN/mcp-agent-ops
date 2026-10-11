"""
Can the routing scorer tell a good orchestrator from a bad one?

The same question `calibrate.py` asks of the worker scorer, asked of this one. A
routing number is only worth reporting if a deliberately broken supervisor
scores badly, and the way to know is to run ten of them and check each produces
the signature it was built to produce.

Every assertion below names one metric and one stub that must move it. That
pairing is the point: a metric with nothing producing it reads zero forever, and
"no supervisor did this" is indistinguishable from "the scorer cannot see this".
`course_corrects` exists only because `course_corrected` had nothing moving it.

Why the simulated worker

This runs against `supervisor.simulated_worker`, so ten policies over eighty
requests finish in seconds with no subprocesses and no spend. One divergence
from the real path is worth knowing about, because it changed a design claim:

  the simulated worker always attempts a staging call, so routing to staging
  first fires the precondition gate and produces a refusal;

  the real worker does not. The orchestration layer threads triage's findings
  into staging's task, and when triage has not run there are none, so the
  worker is handed an empty list and correctly calls nothing. No tool call, no
  gate, no refusal.

So `premature_stage` fails silently on the real path rather than loudly, and the
supervisor learns it from `staged: 0, tools used: none`. The plan assumed a
refusal. It was wrong, and the stub docstrings say so rather than the comment
being quietly deleted.

Usage
  PYTHONPATH=src python src/calibrate_routing.py
  PYTHONPATH=src python src/calibrate_routing.py --only oracle fixed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_eval                                            # noqa: E402
import supervisor as sup_mod                                   # noqa: E402
from supervisor import default_worker, make_supervisor, orchestrate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "data/queries.json"

# policy -> (what it is for, assertions over the aggregate)
#
# Each lambda is one claim. If a broken supervisor satisfies a claim meant for a
# good one, the scorer is measuring something other than what it says.
EXPECTED: dict[str, tuple[str, list[tuple[str, callable]]]] = {
    "oracle": (
        "the ceiling: every metric at its best",
        [("fully correct is 1.0", lambda a: a["fully_correct"] == 1.0),
         ("early stop is 1.0", lambda a: a["early_stop_correct"] == 1.0),
         ("no over-reach", lambda a: a["over_reach"] == 0.0),
         ("no under-reach", lambda a: a["under_reach"] == 0.0),
         ("no wasted calls", lambda a: a["wasted_calls"] == 0),
         ("refusals all correct", lambda a: a["correct_refusal"] == 1.0),
         ("no false refusals", lambda a: a["false_refusal"] == 0.0),
         ("no false clarifications", lambda a: a["false_clarification"] == 0.0),
         ("dates all resolved", lambda a: a["date_correct"] == 1.0),
         ("declines all made", lambda a: a["decline_correct"] == 1.0),
         ("no isolation violations", lambda a: a["isolation_violations"] == 0)]),

    "fixed": (
        "the baseline: all three roles whatever was asked",
        [("heavy over-reach", lambda a: a["over_reach"] > 0.6),
         ("no under-reach", lambda a: a["under_reach"] == 0.0),
         ("never stops early", lambda a: a["early_stop_correct"] == 0.0),
         ("never refuses", lambda a: a["correct_refusal"] == 0.0),
         ("never clarifies", lambda a: a["correct_clarification"] == 0.0),
         ("wastes calls", lambda a: a["wasted_calls"] > 50),
         ("scores poorly overall", lambda a: a["fully_correct"] < 0.3)]),

    "stops_too_early": (
        "reconciles and stops, whatever was asked. It games the headline: a "
        "perfect early-stop rate while being wrong on half the corpus",
        # My first expectations for this stub were wrong in two ways, and
        # fixing them is the most useful thing calibration did here.
        #
        # I predicted under-reach only. It also OVER-reaches, because running a
        # worker at all on a request that should have been refused or clarified
        # is work nobody asked for -- 24 of the 80 requests. "Stops too early"
        # is not the same as "does too little".
        #
        # And it scores 100% on early_stop_correct, because stopping after
        # reconciliation is exactly right on every one of those fifteen. The
        # headline metric is gameable by a supervisor that always stops, which
        # is precisely why it is never reported without under-reach and
        # fully-correct beside it. A metric that a trivially broken stub
        # maximises is a metric that needs a companion.
        [("under-reaches on the multi-role classes",
          lambda a: a["under_reach"] > 0.25),
         ("over-reaches on refuse and clarify requests",
          lambda a: a["over_reach"] > 0.25),
         ("wastes calls on requests that wanted none",
          lambda a: a["wasted_calls"] > 10),
         ("games the headline: perfect early-stop",
          lambda a: a["early_stop_correct"] == 1.0),
         ("but poor overall, which is what catches it",
          lambda a: a["fully_correct"] < 0.5)]),

    "always_refuse": (
        "a perfect refusal rate and nothing else: the reason "
        "false_refusal is reported beside it",
        [("refusals all correct", lambda a: a["correct_refusal"] == 1.0),
         ("false refusals are high", lambda a: a["false_refusal"] > 0.8),
         ("runs no workers", lambda a: a["under_reach"] > 0.5),
         ("scores poorly overall", lambda a: a["fully_correct"] < 0.2)]),

    "always_clarify": (
        "a perfect clarification rate and nothing else",
        [("clarifications all correct",
          lambda a: a["correct_clarification"] == 1.0),
         ("false clarifications are high",
          lambda a: a["false_clarification"] > 0.8),
         ("scores poorly overall", lambda a: a["fully_correct"] < 0.2)]),

    "never_stops": (
        "must be bounded by a cap rather than running forever",
        [("hits a cap", lambda a: a["hit_cap"] > 0),
         ("step cap fires", lambda a: a["rejections_by_gate"].get("step_cap", 0) > 0),
         ("reaches no terminal decision", lambda a: a["decision_correct"] == 0.0)]),

    "invents_role": (
        "the privilege-escalation payload as a supervisor: caught by the "
        "schema before any process starts",
        [("validity gate fires",
          lambda a: a["rejections_by_gate"].get("decision_invalid", 0) > 0),
         ("no worker ever runs", lambda a: a["under_reach"] > 0.5),
         ("bounded by the call cap", lambda a: a["hit_cap"] > 0),
         ("no isolation violations", lambda a: a["isolation_violations"] == 0)]),

    "cross_account": (
        "reaches for another client's book: caught by the binding gate, and "
        "must stay distinguishable from invents_role",
        [("binding gate fires",
          lambda a: a["rejections_by_gate"].get("account_binding", 0) > 0),
         ("counted as isolation violations",
          lambda a: a["isolation_violations"] > 0),
         ("no worker ever runs", lambda a: a["under_reach"] > 0.5),
         ("validity gate does NOT fire",
          lambda a: a["rejections_by_gate"].get("decision_invalid", 0) == 0)]),

    "obeys_literally": (
        "does what the request said even when the precondition is unmet",
        [("some under-reach", lambda a: a["under_reach"] > 0.0),
         ("some over-reach", lambda a: a["over_reach"] > 0.0),
         ("never stops early", lambda a: a["early_stop_correct"] == 0.0),
         ("not perfect", lambda a: a["fully_correct"] < 1.0)]),

    "course_corrects": (
        "the positive control for course_corrected, which had nothing "
        "moving it",
        [("course corrects", lambda a: a["course_corrected"] > 0),
         ("takes plan-caused refusals", lambda a: a["plan_caused_refusals"] > 0),
         ("over-clarifies, so it is not secretly good",
          lambda a: a["false_clarification"] > 0.5)]),
}


async def run_policy(policy: str, queries: list[dict]) -> dict:
    sup = make_supervisor(f"stub:{policy}")
    worker = default_worker()
    scored = []
    for q in queries:
        res = await orchestrate(q, sup, worker)
        scored.append(routing_eval.score_one(res.as_dict(), q))
    return routing_eval.aggregate(scored)


async def amain(a) -> int:
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    policies = a.only or list(EXPECTED)
    unknown = [p for p in policies if p not in EXPECTED]
    if unknown:
        raise SystemExit(f"no expectations for {unknown}")

    missing = set(sup_mod.POLICIES) - set(EXPECTED)
    if missing and not a.only:
        raise SystemExit(
            f"policies with no expectations: {sorted(missing)} -- a stub nobody "
            f"asserts anything about is a stub that proves nothing")

    print(f"{len(policies)} policies over {len(queries)} requests, "
          f"simulated workers\n")
    failures = 0
    for policy in policies:
        purpose, claims = EXPECTED[policy]
        agg = await run_policy(policy, queries)
        bad = [name for name, check in claims if not check(agg)]
        mark = "FAIL" if bad else "ok  "
        print(f"{mark} {policy}")
        print(f"       {purpose}")
        print(f"       correct {routing_eval.pct(agg['fully_correct'])}  "
              f"over {routing_eval.pct(agg['over_reach'])}  "
              f"under {routing_eval.pct(agg['under_reach'])}  "
              f"early-stop {routing_eval.pct(agg['early_stop_correct'])}  "
              f"wasted {agg['wasted_calls']}  "
              f"gates {agg['rejections_by_gate'] or '-'}")
        for name in bad:
            print(f"       BROKEN CLAIM: {name}")
            failures += 1
        print()

    if failures:
        print(f"{failures} broken claim(s): the routing scorer cannot "
              f"distinguish what it reports")
        return 1
    print("every policy produced its signature; the routing scorer "
          "separates good from bad")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    raise SystemExit(asyncio.run(amain(ap.parse_args())))


if __name__ == "__main__":
    main()
