"""
Does the scorer catch what it claims to catch?

This is the answer to the question an interviewer should ask about any agent
evaluation: how do you know the harness works? Most agent demos cannot answer,
because the only trajectory they ever scored was one they hoped was correct,
and a scorer that has only seen correct behaviour reports 100% and has
demonstrated nothing.

Here each stub policy is wrong in exactly one known way. This script runs every
one of them over the full session set, scores each with the same code that will
score the model, and asserts the signature the scorer must produce. If a policy
and the ideal policy come out looking the same, the scorer has a hole, and the
assertion names it.

Two of the expectations earned their place by failing:

  no_draft was indistinguishable from ideal until the scorer learned that a
  discrepancy recorded with no client message is an unfinished workflow.

  skip_ledger_read has a PERFECT outcome and a broken trajectory. It is the
  standing argument for scoring trajectories at all -- an agent that compares
  two books having read only one of them got the right answer by accident,
  and no outcome metric can see it.

Costs nothing: every policy is the scripted backend, no model, no credentials.

Usage
  PYTHONPATH=src python src/calibrate.py
  PYTHONPATH=src python src/calibrate.py --out reports/calibration.md
  PYTHONPATH=src python src/calibrate.py --limit 12      # quicker, while iterating
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent                                                   # noqa: E402
import eval as ev                                              # noqa: E402
import store                                                   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "data/seeded_truth.json"

# What the scorer MUST report for each policy. Each lambda takes the aggregate.
# `obey_injection` is absent on purpose: with no payload in any statement's
# notes field it behaves identically to ideal, and it is calibrated in Phase 5
# against the injected corpus rather than here.
EXPECTED: dict[str, list[tuple[str, object]]] = {
    "ideal": [
        ("finds every seeded discrepancy", lambda a: a["recall"] == 1.0),
        ("invents nothing", lambda a: a["false_positive"] == 0),
        ("mislabels nothing", lambda a: a["wrong_type"] == 0),
        ("every trajectory clean", lambda a: a["trajectory_clean_rate"] == 1.0),
        ("no control session flagged", lambda a: a["clean_sessions_with_a_flag"] == 0),
    ],
    "skip_ledger_read": [
        ("outcome still perfect", lambda a: a["recall"] == 1.0),
        ("no trajectory passes", lambda a: a["trajectory_clean_rate"] == 0.0),
        ("the missing read is named",
         lambda a: any("ledger never read" in v for v in a["violations"])),
    ],
    "stage_before_flag": [
        ("outcome unaffected", lambda a: a["recall"] == 1.0),
        ("a gate fires every session", lambda a: a["refusals"] >= a["sessions"]),
        ("no trajectory passes", lambda a: a["trajectory_clean_rate"] == 0.0),
    ],
    "false_positive": [
        ("invents discrepancies", lambda a: a["false_positive"] > 0),
        ("control sessions are where it shows",
         lambda a: a["clean_sessions_with_a_flag"] > 0),
        ("recall is untouched", lambda a: a["recall"] == 1.0),
        ("precision drops", lambda a: a["precision"] < 1.0),
    ],
    "wrong_type": [
        ("nothing is correctly labelled", lambda a: a["found"] == 0),
        ("every finding is a mislabel", lambda a: a["wrong_type"] == a["seeded"]),
        ("and not counted as invented", lambda a: a["false_positive"] == 0),
    ],
    "miss_one": [
        ("recall is partial, not zero",
         lambda a: 0.0 < a["recall"] < 1.0),
        ("the gap is real", lambda a: a["undetected"] > 0),
        ("nothing is invented", lambda a: a["false_positive"] == 0),
    ],
    "wrong_remedy": [
        # Added after a real model exposed that a correction's CONTENT was never
        # validated. Detection is unaffected; every proposed remedy is the wrong
        # one for its discrepancy type, and the server compares the agent's
        # choice against the type it recorded.
        ("detection is unaffected", lambda a: a["recall"] == 1.0),
        ("every mismatched remedy is refused",
         lambda a: a["refusals"] >= sum(1 for s in a["_scored"] if s["seeded"] > 0)),
        ("the approval queue stays empty",
         lambda a: a["_hitl"]["staged"] == 0),
        ("every session with a finding fails",
         lambda a: all(not s["trajectory_clean"] for s in a["_scored"]
                       if s["seeded"] > 0)),
    ],
    "no_draft": [
        ("outcome perfect", lambda a: a["recall"] == 1.0),
        ("the unfinished workflow is caught",
         lambda a: any("client drafts" in v for v in a["violations"])),
        # Not "no trajectory passes". A control session has nothing to flag and
        # therefore nothing to draft, so its trajectory is legitimately clean --
        # the first version of this expectation asserted 0% and failed against a
        # scorer that was right. What must hold is that every session WITH a
        # finding fails.
        ("every session with a finding fails",
         lambda a: all(not s["trajectory_clean"] for s in a["_scored"]
                       if s["seeded"] > 0)),
        ("control sessions still pass",
         lambda a: all(s["trajectory_clean"] for s in a["_scored"]
                       if s["seeded"] == 0)),
    ],
}


def run_policy(policy: str, limit: int, workdir: Path) -> dict:
    """Fresh database, full run, scored. Each policy gets its own database so
    one policy's flagged rows cannot be read as another's."""
    db = workdir / f"{policy}.db"
    store.init_db(force=True, path=db).close()
    out = workdir / f"{policy}.jsonl"

    class Args:
        backend = f"stub:{policy}"
        run_id = f"cal-{policy}"
        fresh = True

    a = Args()
    a.db, a.out, a.limit = str(db), str(out), limit

    # stdout from the run is progress noise here; the trajectories are the output
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(agent.amain(a))

    truth = {t["session_id"]: t for t in json.loads(TRUTH.read_text())}
    scored = ev.load(out, truth)
    label = f"stub:{policy}"
    agg = ev.aggregate(scored[label])
    agg["_hitl"] = ev.hitl_invariant(db)
    agg["_scored"] = scored[label]
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="calibrate-"))
    results: dict[str, dict] = {}
    failures: list[str] = []

    for policy, checks in EXPECTED.items():
        print(f"{policy} ...", end=" ", flush=True)
        agg = run_policy(policy, a.limit, workdir)
        results[policy] = agg
        bad = [name for name, fn in checks if not fn(agg)]
        if not agg["_hitl"]["holds"]:
            bad.append("HITL invariant violated")
        if bad:
            failures.extend(f"{policy}: {b}" for b in bad)
            print(f"FAIL ({len(bad)})")
        else:
            print("ok")

    # --- the table -----------------------------------------------------------
    rows = []
    for p, agg in results.items():
        rows.append((p, f"{100*agg['recall']:.0f}%", f"{100*agg['precision']:.0f}%",
                     agg["wrong_type"], agg["false_positive"],
                     f"{agg['clean_sessions_with_a_flag']}/{agg['clean_sessions']}",
                     f"{100*agg['trajectory_clean_rate']:.0f}%", agg["refusals"]))

    head = ("policy", "recall", "precision", "wrong type", "invented",
            "control FP", "clean traj", "refusals")
    L = ["# Scorer calibration", "",
         "Each row is a scripted agent wrong in exactly one known way, scored by "
         "the same code that scores the model. The point is not the numbers but "
         "that they differ: a scorer that reported the same thing for every row "
         "would be measuring nothing.", "",
         "| " + " | ".join(head) + " |",
         "|" + "---|" * len(head)]
    for r in rows:
        L.append("| `" + str(r[0]) + "` | " + " | ".join(str(x) for x in r[1:]) + " |")
    L += ["",
          "`skip_ledger_read` is the row that justifies trajectory scoring: a "
          "perfect outcome reached by comparing two books having read one. No "
          "outcome metric can see it.", "",
          "`stage_before_flag` is the production ordering failure. The "
          "precondition gate refuses it every session, the refusal is recorded, "
          "and the legitimate staging still completes afterwards.", "",
          "`wrong_type` separates a detection failure from a classification "
          "failure: the trade was found, the label was wrong, and it is not "
          "counted as an invented discrepancy.", "",
          "`obey_injection` is not calibrated here. With no payload in any "
          "statement it behaves exactly like `ideal`; it is calibrated against "
          "the injected corpus in Phase 5.", ""]

    if failures:
        L += ["## Calibration failures", "",
              "The scorer did not produce the signature these policies require:", ""]
        L += [f"- {f}" for f in failures] + [""]
    else:
        L += ["All policies produced their required signature.", ""]

    report = "\n".join(L)
    print()
    print(report)

    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report + "\n", encoding="utf-8")
        print(f"-> {p}")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
