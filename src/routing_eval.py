"""
Scoring the orchestrator: did it route to the right workers, and stop in time?

The existing `eval.py` scores what the workers found -- recall, precision,
mislabels. This scores the layer above: given a request in English, did the
supervisor invoke the workers that request and those books together called for,
and nothing more.

Four metrics are reported in pairs, on purpose. Reporting only one side of each
is how a routing number flatters a system:

  over-reach / under-reach        a supervisor that runs everything scores well
                                  on recall of needed roles; one that runs
                                  nothing scores well on not wasting work.

  refusal / false refusal         refusing everything gives a perfect refusal
                                  rate on the out-of-scope class.

  clarification / false           asking about everything gives a perfect
  clarification                   clarification rate. A supervisor that always
                                  asks is as useless as one that never does,
                                  and only one of the two is usually measured.

  relayed refusal / plan-caused   a worker refusal is a success when the client
  refusal                         asked about a date with no statement, and a
                                  failure when the plan skipped a precondition.
                                  Same event, opposite meaning, so ground
                                  truth's `stop_cause` separates them rather
                                  than a heuristic.

The headline is `early_stop_correct`. Fifteen of the eighty requests let the
orchestrator stop short of what was asked -- a full reconciliation whose books
agree, or a date with no statement loaded. A supervisor that commits to a route
before any worker has read anything cannot win one of them, whatever its
reasoning budget, because at the moment it commits nobody has looked. That is
an information-ordering difference and it is the whole case for deciding each
step.

Usage
  PYTHONPATH=src python src/routing_eval.py
  PYTHONPATH=src python src/routing_eval.py --compare stub:oracle stub:fixed
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "data/queries.json"
RUNS = ROOT / "reports/requests.jsonl"


# --- one request --------------------------------------------------------------

def score_one(rec: dict, q: dict) -> dict:
    """Score one request against its ground truth.

    `rec` is a `supervisor.Result` as serialised; `q` is the corpus record.
    """
    e = q["expect"]
    want = e["roles"]
    got = rec["invoked"]
    want_end = "done" if e["decision"] == "run" else e["decision"]
    end = rec.get("terminal_action")

    # Over-reach is any role beyond what was called for, by kind or by count:
    # running reconciliation twice is wasted work even though the role is right.
    extra_kinds = set(got) - set(want)
    missing_kinds = set(want) - set(got)
    over = bool(extra_kinds) or len(got) > len(want)
    under = bool(missing_kinds)

    refusals = rec.get("worker_refusals", 0)
    relayed = e["stop_cause"] == "no_statement"

    declines = e["declines"] if e["decision"] == "run" else []
    declared = rec.get("declined") or []
    message = (rec.get("terminal_message") or "")
    decline_ok = (not declines) or all(
        d in declared or d in message for d in declines)

    return {
        "query_id": rec["query_id"],
        "intent": q["intent"],
        "supervisor": rec["supervisor"],

        "exact_route": got == want,
        "decision_correct": end == want_end,
        "fully_correct": got == want and end == want_end and decline_ok,
        "over_reach": over,
        "under_reach": under,
        "wasted_calls": max(0, len(got) - len(want)),

        # the headline
        "early_stop_opportunity": e["early_stop"],
        "early_stop_correct": e["early_stop"] and got == want,
        "stop_cause": e["stop_cause"],

        "should_refuse": e["decision"] == "refuse",
        "did_refuse": end == "refuse",
        "should_clarify": e["decision"] == "clarify",
        "did_clarify": end == "clarify",
        "clarify_about": e["clarify_about"],

        "decline_expected": bool(declines),
        "decline_correct": bool(declines) and decline_ok,

        # Scored only where the request was supposed to be served. A refused
        # request has no date to resolve, and the first version marked the
        # oracle wrong on all twelve out-of-scope requests for correctly
        # refusing them before resolving anything.
        "date_scored": e["decision"] == "run",
        "date_expected": e["session_date"],
        "date_resolved": rec.get("resolved_date"),
        "date_correct": (e["decision"] == "run"
                         and rec.get("resolved_date") == e["session_date"]),

        "worker_refusals": refusals,
        "refusal_relayed": relayed and refusals > 0 and got == want,
        "plan_caused_refusals": 0 if relayed else refusals,
        # Genuine recovery: a role was refused and a later call to that same
        # role succeeded. The first version counted "refusals happened and the
        # route was right", which credited the fixed pipeline with eighteen
        # recoveries it had not made -- it never re-plans, it just collects
        # refusals on its way through a route that sometimes matches.
        # Two different things, named apart because they are.
        #
        # `recovered` is the strict one: a role was refused and a later call to
        # that same role succeeded. A retry that worked.
        #
        # `course_corrected` is changing approach after a refusal -- asking
        # instead of pressing on -- and ending with the right decision anyway.
        # Both are reported because a supervisor can do either, and a single
        # "recovered" number would hide which.
        "recovered": bool(rec.get("recovered_roles")),
        "recovered_roles": rec.get("recovered_roles") or [],
        "course_corrected": (refusals > 0 and not relayed
                             and end == want_end),

        "rejections": rec.get("rejections_by_gate") or {},
        "isolation_violation": bool(
            (rec.get("rejections_by_gate") or {}).get("account_binding")),
        "supervisor_calls": rec.get("supervisor_calls", 0),
        "stopped": rec.get("stopped"),
    }


# --- aggregate ----------------------------------------------------------------

def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def aggregate(scored: list[dict]) -> dict:
    n = len(scored)
    opp = [s for s in scored if s["early_stop_opportunity"]]
    ref = [s for s in scored if s["should_refuse"]]
    cla = [s for s in scored if s["should_clarify"]]
    dec = [s for s in scored if s["decline_expected"]]
    dated = [s for s in scored if s["date_scored"]]

    gates: dict[str, int] = {}
    for s in scored:
        for g, c in s["rejections"].items():
            gates[g] = gates.get(g, 0) + c

    return {
        "n": n,
        "exact_route": _rate(sum(s["exact_route"] for s in scored), n),
        "fully_correct": _rate(sum(s["fully_correct"] for s in scored), n),
        "decision_correct": _rate(sum(s["decision_correct"] for s in scored), n),

        "over_reach": _rate(sum(s["over_reach"] for s in scored), n),
        "under_reach": _rate(sum(s["under_reach"] for s in scored), n),
        "wasted_calls": sum(s["wasted_calls"] for s in scored),

        "early_stop_n": len(opp),
        "early_stop_correct": _rate(sum(s["early_stop_correct"] for s in opp),
                                    len(opp)),

        "refusal_n": len(ref),
        "correct_refusal": _rate(sum(s["did_refuse"] for s in ref), len(ref)),
        # The other side of the pair. A supervisor that refuses everything has a
        # perfect correct_refusal rate and is useless.
        "false_refusal": _rate(
            sum(s["did_refuse"] and not s["should_refuse"] for s in scored),
            n - len(ref)),

        "clarify_n": len(cla),
        "correct_clarification": _rate(sum(s["did_clarify"] for s in cla),
                                       len(cla)),
        "false_clarification": _rate(
            sum(s["did_clarify"] and not s["should_clarify"] for s in scored),
            n - len(cla)),

        "decline_n": len(dec),
        "decline_correct": _rate(sum(s["decline_correct"] for s in dec), len(dec)),

        "date_n": len(dated),
        "date_correct": _rate(sum(s["date_correct"] for s in dated), len(dated)),

        "refusals_relayed": sum(s["refusal_relayed"] for s in scored),
        "plan_caused_refusals": sum(s["plan_caused_refusals"] for s in scored),
        "recovered": sum(s["recovered"] for s in scored),
        "course_corrected": sum(s["course_corrected"] for s in scored),

        "rejections_by_gate": gates,
        "isolation_violations": sum(s["isolation_violation"] for s in scored),
        "supervisor_calls": sum(s["supervisor_calls"] for s in scored),
        "hit_cap": sum(s["stopped"] == "supervisor_cap" for s in scored),
        "backend_errors": sum(s["stopped"] == "supervisor_error" for s in scored),
    }


def by_intent(scored: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for s in scored:
        groups.setdefault(s["intent"], []).append(s)
    return {k: aggregate(v) for k, v in sorted(groups.items())}


# --- head to head -------------------------------------------------------------

def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar on the discordant pairs. Same function as
    eval.py's, repeated rather than imported so this module stands alone when
    only routing is being looked at."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def compare(a: list[dict], b: list[dict], label_a: str, label_b: str) -> dict:
    """Paired comparison on `fully_correct`, over the requests both arms ran."""
    by_a = {s["query_id"]: s for s in a}
    by_b = {s["query_id"]: s for s in b}
    shared = sorted(set(by_a) & set(by_b))

    a_only = sum(1 for q in shared
                 if by_a[q]["fully_correct"] and not by_b[q]["fully_correct"])
    b_only = sum(1 for q in shared
                 if by_b[q]["fully_correct"] and not by_a[q]["fully_correct"])

    early = [q for q in shared if by_a[q]["early_stop_opportunity"]]
    a_early = sum(1 for q in early if by_a[q]["early_stop_correct"])
    b_early = sum(1 for q in early if by_b[q]["early_stop_correct"])

    return {
        "label_a": label_a, "label_b": label_b, "paired": len(shared),
        "a_only": a_only, "b_only": b_only,
        "discordant": a_only + b_only,
        "p": mcnemar_exact(a_only, b_only),
        "early_stop_n": len(early), "a_early": a_early, "b_early": b_early,
    }


# --- render -------------------------------------------------------------------

def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render(by_sup: dict[str, dict], intents: dict[str, dict[str, dict]],
           head: dict | None) -> str:
    L = ["# Routing", ""]
    L.append("What the supervisor decided, scored against what the request and "
             "the books together called for.")
    L.append("")

    L.append("| supervisor | n | fully correct | exact route | over | under | "
             "early stop | wasted calls |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s, a in by_sup.items():
        L.append(f"| `{s}` | {a['n']} | {pct(a['fully_correct'])} | "
                 f"{pct(a['exact_route'])} | {pct(a['over_reach'])} | "
                 f"{pct(a['under_reach'])} | "
                 f"{pct(a['early_stop_correct'])} ({a['early_stop_n']}) | "
                 f"{a['wasted_calls']} |")
    L.append("")

    L.append("Both sides of each pair, because reporting one side is how a "
             "routing number flatters a system: a supervisor that refuses "
             "everything has a perfect refusal rate, and one that asks about "
             "everything has a perfect clarification rate.")
    L.append("")
    L.append("| supervisor | refuse | false refuse | clarify | false clarify | "
             "date | decline | relayed | plan-caused | retried ok | corrected |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for s, a in by_sup.items():
        L.append(f"| `{s}` | {pct(a['correct_refusal'])} | "
                 f"{pct(a['false_refusal'])} | {pct(a['correct_clarification'])} | "
                 f"{pct(a['false_clarification'])} | {pct(a['date_correct'])} | "
                 f"{pct(a['decline_correct'])} | {a['refusals_relayed']} | "
                 f"{a['plan_caused_refusals']} | {a['recovered']} | "
                 f"{a['course_corrected']} |")
    L.append("")

    L.append("| supervisor | model calls | hit cap | backend errors | "
             "isolation violations | rejections by gate |")
    L.append("|---|---|---|---|---|---|")
    for s, a in by_sup.items():
        g = ", ".join(f"{k} {v}" for k, v in sorted(
            a["rejections_by_gate"].items())) or "none"
        L.append(f"| `{s}` | {a['supervisor_calls']} | {a['hit_cap']} | "
                 f"{a['backend_errors']} | {a['isolation_violations']} | {g} |")
    L.append("")

    for s, groups in intents.items():
        L.append(f"## `{s}` by intent")
        L.append("")
        L.append("| intent | n | fully correct | exact route | early stop |")
        L.append("|---|---|---|---|---|")
        for intent, a in groups.items():
            es = (f"{pct(a['early_stop_correct'])} ({a['early_stop_n']})"
                  if a["early_stop_n"] else "-")
            L.append(f"| {intent} | {a['n']} | {pct(a['fully_correct'])} | "
                     f"{pct(a['exact_route'])} | {es} |")
        L.append("")

    if head:
        h = head
        L += ["## Head to head", "",
              f"`{h['label_a']}` against `{h['label_b']}`, {h['paired']} paired "
              f"requests.", "",
              f"- discordant pairs: {h['discordant']} "
              f"({h['label_a']} only {h['a_only']}, "
              f"{h['label_b']} only {h['b_only']})",
              f"- exact two-sided McNemar: p = {h['p']:.4f}",
              f"- early-stop class ({h['early_stop_n']} requests): "
              f"{h['label_a']} {h['a_early']}, {h['label_b']} {h['b_early']}",
              ""]
        if h["early_stop_n"] and h["a_early"] and not h["b_early"]:
            L += ["The early-stop split is the one worth reading. A supervisor "
                  "that commits to a route before any worker has read anything "
                  "cannot win those requests whatever its reasoning budget: at "
                  "the moment it commits, nobody has looked at the books. That "
                  "is an information-ordering difference, not a prompt-quality "
                  "one.", ""]
    return "\n".join(L)


# --- io -----------------------------------------------------------------------

def load(runs: Path, queries: Path) -> dict[str, list[dict]]:
    """Group scored records by arm, where an arm is (supervisor, run_id).

    The run_id is part of the key, and leaving it out cost a measurement.

    The earlier version keyed on (supervisor, query_id) with first-write-wins,
    on the reasoning that a resumed run appends and the first record is the
    reproducible one. That is true for a resume and false for a re-run: two runs
    of the same supervisor land in one file under one label, and the scorer
    silently reported the OLDER one. A fix to the harness was measured, scored,
    and came back with the pre-fix numbers -- 399 model calls and 61 invalid
    decisions, which were the previous run's. Nothing in the report said so.

    So an arm is now a supervisor AND a run, every run is reported, and nothing
    is discarded quietly. Where a supervisor has several runs the caller is told
    rather than being handed whichever one the parser happened to meet first.
    """
    qs = {q["query_id"]: q for q in json.loads(
        queries.read_text(encoding="utf-8"))}
    out: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    runs_per_sup: dict[str, list[str]] = {}

    for line in runs.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        run_id = rec.get("run_id") or "unlabelled"
        sup = rec["supervisor"]
        arm = sup if run_id == "unlabelled" else f"{sup} @ {run_id}"

        key = (arm, rec["query_id"])
        if key in seen:
            continue            # within one run, a resume appends; the first
        seen.add(key)           # record of a query is the reproducible one
        q = qs.get(rec["query_id"])
        if q is None:
            continue
        out.setdefault(arm, []).append(score_one(rec, q))
        if run_id not in runs_per_sup.setdefault(sup, []):
            runs_per_sup[sup].append(run_id)

    for sup, ids in runs_per_sup.items():
        if len(ids) > 1:
            print(f"note: `{sup}` has {len(ids)} runs in this file "
                  f"({', '.join(ids)}). Each is reported separately below. "
                  f"They are not merged, and the newest is not assumed to be "
                  f"the one you want.", file=sys.stderr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=str(RUNS))
    ap.add_argument("--queries", default=str(QUERIES))
    ap.add_argument("--out", default=str(ROOT / "reports/routing.md"))
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None)
    a = ap.parse_args()

    runs = Path(a.runs)
    if not runs.exists():
        raise SystemExit(f"{runs} does not exist -- run src/run_requests.py first")

    scored = load(runs, Path(a.queries))
    if not scored:
        raise SystemExit("no scoreable records")

    by_sup = {s: aggregate(v) for s, v in sorted(scored.items())}
    intents = {s: by_intent(v) for s, v in sorted(scored.items())}

    head = None
    if a.compare:
        x, y = a.compare
        missing = [s for s in (x, y) if s not in scored]
        if missing:
            raise SystemExit(f"no records for {missing}; have "
                             f"{sorted(scored)}")
        head = compare(scored[x], scored[y], x, y)

    text = render(by_sup, intents, head)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
