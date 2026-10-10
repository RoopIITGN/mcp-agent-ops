"""
Scoring: what the agent found, how it got there, and which gates fired.

Ground truth is data/seeded_truth.json and nothing else. In particular it is
NOT backends.classify -- that function is the stub agent's own judgement, and
scoring an agent against its own classifier would make the ideal policy
perfect by construction and measure nothing. Because truth is independent, the
ideal stub reaching 52 of 52 is a finding: it says the classification rules
agree with how the data was seeded.

Three scores, and a fourth thing that is not a score.

  Outcome accuracy. Per discrepancy type, against seeded truth: found, missed,
  invented. Reported by type rather than blended, because a system that finds
  every duplicate booking and no settlement shift has a shape that one
  aggregate number hides. The false-positive rate is computed on the clean
  sessions separately -- an agent that flags something on every account looks
  excellent until the controls are counted.

  Right trade, wrong type is its own row. It is a false positive and a false
  negative at once, but calling it two independent errors overstates the
  damage: the agent did find the trade. It is the signature of a
  classification failure rather than a detection failure, and the two have
  different fixes.

  Trajectory accuracy. Scored against what the session required, not against
  the agent's own plan: both books read before comparing, one flag per seeded
  discrepancy, one staged correction per flag, no refused calls. An agent that
  arrives at the right answer through a refused call is not working, and
  outcome accuracy alone cannot see that -- which is exactly what the
  skip_ledger_read policy demonstrates, since it has a perfect outcome and a
  broken trajectory.

  Structured-output validity. The fraction of tool calls that were accepted
  rather than refused for a schema reason. Near-meaningless under the stub,
  which emits valid calls by construction; it is the number that moves when a
  model is driving and it is the public counterpart to the CV's 87% -> 99.4%.

  And the HITL invariant, which is not a score because a score implies a
  tolerable failure rate. Every staged action must be PENDING_HUMAN with no
  decider. If that is ever false the system is broken, not worse.

Usage
  PYTHONPATH=src python src/eval.py
  PYTHONPATH=src python src/eval.py --backend stub:ideal
  PYTHONPATH=src python src/eval.py --compare stub:ideal stub:miss_one
  PYTHONPATH=src python src/eval.py --out reports/eval.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store                                                   # noqa: E402
import tools                                                   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "data/seeded_truth.json"
TRAJ = ROOT / "reports/trajectories.jsonl"
KINDS = tools.DISCREPANCY_TYPES


# --- statistics, without scipy ------------------------------------------------

def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar on the discordant pairs only.

    b and c are the counts of sessions where exactly one of the two systems
    succeeded. Concordant pairs carry no information about a difference and are
    deliberately excluded -- which is the whole point of a paired test, and why
    comparing two aggregate percentages is weaker.

    Exact rather than chi-square because the discordant count here is small
    (tens, not hundreds), and the chi-square approximation is unreliable there.
    Implemented from math.comb so the CI gate needs no scientific stack.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def bootstrap_ci(values: list[float], iters: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap. Deterministic given the seed, so a committed
    report can be recomputed to the same digits."""
    import random
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(alpha / 2 * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


# --- scoring one session ------------------------------------------------------

def score_session(rec: dict, truth: dict) -> dict:
    """Compare one trajectory against what was seeded for that session."""
    seeded = {(d["trade_id"], d["type"]) for d in truth["discrepancies"]}
    seeded_trades = {d["trade_id"] for d in truth["discrepancies"]}
    seeded_type = {d["trade_id"]: d["type"] for d in truth["discrepancies"]}

    flagged = {(f["trade_id"], f["type"]) for f in rec["flagged"]}
    flagged_trades = {f["trade_id"] for f in rec["flagged"]}

    found = seeded & flagged
    missed = seeded - flagged
    invented = flagged - seeded

    # split the invented ones: a real trade with the wrong label is a different
    # failure from a trade that had nothing wrong with it
    wrong_type = {(t, k) for t, k in invented if t in seeded_trades}
    false_positive = {(t, k) for t, k in invented if t not in seeded_trades}
    # a wrong-type flag also accounts for one of the misses, so do not
    # double-count that trade as simply undetected
    undetected = {(t, k) for t, k in missed if t not in flagged_trades}

    # --- trajectory ----------------------------------------------------------
    traj = rec["trajectory"]
    calls = {role: [t["name"] for t in turns if t["kind"] == "call"]
             for role, turns in traj.items()}
    errors = [(role, t.get("name", ""), t.get("text", ""))
              for role, turns in traj.items() for t in turns
              if t["kind"] == "error"]

    violations = []
    recon = calls.get("reconciliation", [])
    if "fetch_broker_statement" not in recon:
        violations.append("statement never read")
    if "fetch_internal_ledger" not in recon:
        violations.append("ledger never read")
    if "calculate_position_deltas" not in recon:
        violations.append("never compared")
    else:
        i = recon.index("calculate_position_deltas")
        if not {"fetch_broker_statement", "fetch_internal_ledger"} <= set(recon[:i]):
            violations.append("compared before reading both books")
    if errors:
        violations.append(f"{len(errors)} refused call(s)")
    triage_calls = calls.get("triage", [])
    n_flag_calls = sum(1 for c in triage_calls if c == "flag_discrepancy")
    n_draft = sum(1 for c in triage_calls if c == "draft_client_inquiry")
    n_stage = sum(1 for c in calls.get("staging", []) if c == "stage_for_execution")
    if n_stage > len(rec["flagged"]):
        violations.append("staged more corrections than discrepancies flagged")
    if n_draft < len(rec["flagged"]):
        # A discrepancy recorded with no client message is an unfinished
        # workflow. Without this check the no_draft policy is indistinguishable
        # from the ideal one, which means the scorer has a hole.
        violations.append("fewer client drafts than discrepancies flagged")

    total_calls = sum(len(v) for v in calls.values())
    schema_refusals = sum(1 for _, _, text in errors if "invalid arguments" in text)

    return {
        "session_id": rec["session_id"],
        "account_id": rec["account_id"],
        "clean_session": not truth["discrepancies"],
        "seeded": len(seeded),
        "found": len(found),
        "undetected": len(undetected),
        "wrong_type": len(wrong_type),
        "false_positive": len(false_positive),
        "found_by_type": Counter(k for _, k in found),
        "undetected_by_type": Counter(k for _, k in undetected),
        "wrong_type_detail": [{"trade_id": t, "flagged_as": k,
                               "actually": seeded_type[t]} for t, k in sorted(wrong_type)],
        "trajectory_clean": not violations,
        "violations": violations,
        "refusals": len(errors),
        "refusal_detail": [{"role": r, "tool": n, "reason": x[:120]}
                           for r, n, x in errors],
        "calls": total_calls,
        "schema_refusals": schema_refusals,
        "flag_calls": n_flag_calls,
        "draft_calls": n_draft,
        "stage_calls": n_stage,
    }


# --- aggregation --------------------------------------------------------------

def aggregate(scored: list[dict]) -> dict:
    seeded = sum(s["seeded"] for s in scored)
    found = sum(s["found"] for s in scored)
    undetected = sum(s["undetected"] for s in scored)
    wrong = sum(s["wrong_type"] for s in scored)
    fp = sum(s["false_positive"] for s in scored)

    clean = [s for s in scored if s["clean_session"]]
    clean_dirty = sum(1 for s in clean if s["false_positive"] or s["wrong_type"])

    by_type: dict[str, dict[str, int]] = {k: {"seeded": 0, "found": 0, "undetected": 0}
                                          for k in KINDS}
    for s in scored:
        for k, n in s["found_by_type"].items():
            by_type.setdefault(k, {"seeded": 0, "found": 0, "undetected": 0})["found"] += n
        for k, n in s["undetected_by_type"].items():
            by_type.setdefault(k, {"seeded": 0, "found": 0, "undetected": 0})["undetected"] += n
    for k in by_type:
        by_type[k]["seeded"] = by_type[k]["found"] + by_type[k]["undetected"]

    calls = sum(s["calls"] for s in scored)
    schema_bad = sum(s["schema_refusals"] for s in scored)

    return {
        "sessions": len(scored),
        "clean_sessions": len(clean),
        "seeded": seeded,
        "found": found,
        "undetected": undetected,
        "wrong_type": wrong,
        "false_positive": fp,
        "recall": found / seeded if seeded else 0.0,
        "precision": found / (found + wrong + fp) if (found + wrong + fp) else 0.0,
        "clean_sessions_with_a_flag": clean_dirty,
        "clean_false_positive_rate": clean_dirty / len(clean) if clean else 0.0,
        "trajectory_clean": sum(1 for s in scored if s["trajectory_clean"]),
        "trajectory_clean_rate": (sum(1 for s in scored if s["trajectory_clean"])
                                  / len(scored) if scored else 0.0),
        "violations": Counter(v for s in scored for v in s["violations"]),
        "refusals": sum(s["refusals"] for s in scored),
        "calls": calls,
        "schema_validity": (calls - schema_bad) / calls if calls else 1.0,
        "by_type": by_type,
    }


def hitl_invariant(db: Path) -> dict:
    """Not a score. Either every staged action awaits a human, or the central
    claim of this repo is false."""
    con = store.connect(db)
    rows = con.execute("SELECT status, decided_by FROM pending_actions").fetchall()
    bad = [dict(r) for r in rows if r["status"] != "PENDING_HUMAN"
           or r["decided_by"] is not None]
    return {"staged": len(rows), "violations": bad, "holds": not bad}


# --- comparison ---------------------------------------------------------------

def compare(a: list[dict], b: list[dict], label_a: str, label_b: str) -> dict:
    """Paired comparison on the sessions both runs covered.

    The unit is a session that was fully correct: every seeded discrepancy
    found, nothing invented, trajectory clean. Pairing on session id is what
    makes the test meaningful -- the sessions differ a lot in difficulty, and
    an unpaired comparison of two averages mostly measures which sessions each
    run happened to get."""
    ok = lambda s: (s["undetected"] == 0 and s["wrong_type"] == 0
                    and s["false_positive"] == 0 and s["trajectory_clean"])
    A = {s["session_id"]: ok(s) for s in a}
    B = {s["session_id"]: ok(s) for s in b}
    shared = sorted(set(A) & set(B))
    only_a = sum(1 for s in shared if A[s] and not B[s])
    only_b = sum(1 for s in shared if B[s] and not A[s])
    return {
        "label_a": label_a, "label_b": label_b, "paired_sessions": len(shared),
        "a_only": only_a, "b_only": only_b,
        "both": sum(1 for s in shared if A[s] and B[s]),
        "neither": sum(1 for s in shared if not A[s] and not B[s]),
        "p_value": mcnemar_exact(only_a, only_b),
    }


# --- variance -----------------------------------------------------------------

def raw_runs(path: Path, backend: str) -> dict[str, dict]:
    """Trajectory records for one backend, keyed by session."""
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["backend"] == backend:
                out[r["session_id"]] = r
    return out


def variance(a: dict[str, dict], b: dict[str, dict], truth: dict) -> dict:
    """How much does the same configuration disagree with itself?

    The floor under every other comparison in this project. A difference
    smaller than this is not a result -- it is the system's own noise. The
    retrieval project measured a 20% flip rate between identical generator
    runs, which is why a 25-to-21 improvement there had to be reported as
    inconclusive rather than as a fix.

    Compared per session on three things, because they can disagree
    independently: the set of (trade, type) findings, how many corrections were
    staged, and whether the trajectory was clean. A run that finds the same
    discrepancies by a different route is stable in outcome and unstable in
    behaviour, and only the second shows up in a trajectory metric.
    """
    shared = sorted(set(a) & set(b))
    rows = []
    for sid in shared:
        fa = {(f["trade_id"], f["type"]) for f in a[sid]["flagged"]}
        fb = {(f["trade_id"], f["type"]) for f in b[sid]["flagged"]}
        sa, sb = len(a[sid]["staged"]), len(b[sid]["staged"])
        ta = score_session(a[sid], truth[sid])["trajectory_clean"]
        tb = score_session(b[sid], truth[sid])["trajectory_clean"]
        rows.append({
            "session_id": sid,
            "findings_match": fa == fb,
            "only_in_a": sorted(fa - fb), "only_in_b": sorted(fb - fa),
            "staged_match": sa == sb, "staged_a": sa, "staged_b": sb,
            "trajectory_match": ta == tb,
            "identical": fa == fb and sa == sb and ta == tb,
        })
    n = len(rows)
    return {
        "sessions": n,
        "identical": sum(1 for r in rows if r["identical"]),
        "findings_flips": sum(1 for r in rows if not r["findings_match"]),
        "staged_flips": sum(1 for r in rows if not r["staged_match"]),
        "trajectory_flips": sum(1 for r in rows if not r["trajectory_match"]),
        "flip_rate": (sum(1 for r in rows if not r["identical"]) / n) if n else 0.0,
        "findings_flip_rate": (sum(1 for r in rows if not r["findings_match"]) / n)
                              if n else 0.0,
        "rows": [r for r in rows if not r["identical"]],
    }


def render_variance(v: dict, backend: str) -> str:
    L = ["# Variance floor", "",
         f"Two runs of `{backend}` over the same sessions, same configuration, "
         f"nothing changed between them. Anything smaller than this is noise, "
         f"not a result.", ""]
    L.append(f"**{v['sessions']} paired session(s). {v['identical']} identical "
             f"in findings, staging and trajectory — a flip rate of "
             f"{pct(v['flip_rate'])}.**")
    L.append("")
    L += ["| measure | sessions that disagreed | rate |", "|---|---|---|",
          f"| findings, as (trade, type) | {v['findings_flips']} | "
          f"{pct(v['findings_flip_rate'])} |",
          f"| corrections staged | {v['staged_flips']} | "
          f"{pct(v['staged_flips'] / v['sessions']) if v['sessions'] else '-'} |",
          f"| trajectory clean | {v['trajectory_flips']} | "
          f"{pct(v['trajectory_flips'] / v['sessions']) if v['sessions'] else '-'} |",
          ""]
    if v["rows"]:
        L += ["## Sessions that disagreed", "",
              "| session | only in run A | only in run B | staged A/B |",
              "|---|---|---|---|"]
        for r in v["rows"][:20]:
            L.append(f"| {r['session_id']} | {r['only_in_a'] or '-'} | "
                     f"{r['only_in_b'] or '-'} | {r['staged_a']}/{r['staged_b']} |")
        L.append("")
    else:
        L += ["No session disagreed with itself. At this sample size the floor "
              "is below 1 in " + str(v["sessions"]) + ", which is the most that "
              "can be said -- a zero observed in 60 trials is not a zero rate. "
              "It does mean every difference reported elsewhere in these results "
              "is larger than the noise this measurement could detect.", ""]
    return "\n".join(L)


# --- report -------------------------------------------------------------------

def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def render(by_backend: dict[str, dict], hitl: dict,
           comparisons: list[dict]) -> str:
    L: list[str] = ["# Evaluation", ""]
    L.append("Scored against `data/seeded_truth.json`. Every number here is "
             "recomputable from `reports/trajectories.jsonl` with no model and "
             "no credentials.")
    L.append("")

    L.append("## Human-in-the-loop invariant")
    L.append("")
    if hitl["holds"]:
        L.append(f"{hitl['staged']} staged action(s), all `PENDING_HUMAN`, none "
                 f"carrying a decider. **Holds.**")
    else:
        L.append(f"**VIOLATED.** {len(hitl['violations'])} staged action(s) left "
                 f"`PENDING_HUMAN` without a human: {hitl['violations']}")
    L.append("")

    L.append("## Outcome and trajectory")
    L.append("")
    L.append("| backend | recall | precision | wrong type | invented | clean-session FP | clean trajectory |")
    L.append("|---|---|---|---|---|---|---|")
    for label, agg in by_backend.items():
        L.append(f"| `{label}` | {pct(agg['recall'])} "
                 f"({agg['found']}/{agg['seeded']}) | {pct(agg['precision'])} | "
                 f"{agg['wrong_type']} | {agg['false_positive']} | "
                 f"{agg['clean_sessions_with_a_flag']}/{agg['clean_sessions']} | "
                 f"{pct(agg['trajectory_clean_rate'])} |")
    L.append("")
    L.append("Recall and precision count `(trade, type)` pairs. *Wrong type* is a "
             "trade that was found and mislabelled; it is excluded from *invented*, "
             "which counts flags on trades where nothing differs. *Clean-session FP* "
             "is how many control sessions got a flag at all -- the number that an "
             "agent flagging everything cannot hide.")
    L.append("")

    for label, agg in by_backend.items():
        L.append(f"### `{label}` by discrepancy type")
        L.append("")
        L.append("| type | seeded | found | missed |")
        L.append("|---|---|---|---|")
        for k in KINDS:
            t = agg["by_type"].get(k, {"seeded": 0, "found": 0, "undetected": 0})
            L.append(f"| {k} | {t['seeded']} | {t['found']} | {t['undetected']} |")
        L.append("")
        if agg["violations"]:
            L.append("Trajectory violations:")
            L.append("")
            for v, n in agg["violations"].most_common():
                L.append(f"- {v} — {n} session(s)")
            L.append("")
        L.append(f"Schema validity {pct(agg['schema_validity'])} over "
                 f"{agg['calls']} tool call(s); {agg['refusals']} refusal(s).")
        L.append("")

    if comparisons:
        L.append("## Paired comparisons")
        L.append("")
        L.append("| A | B | A only | B only | both | neither | p |")
        L.append("|---|---|---|---|---|---|---|")
        for c in comparisons:
            L.append(f"| `{c['label_a']}` | `{c['label_b']}` | {c['a_only']} | "
                     f"{c['b_only']} | {c['both']} | {c['neither']} | "
                     f"{c['p_value']:.4f} |")
        L.append("")
        L.append("Exact two-sided McNemar on discordant pairs. A session counts as "
                 "a success only if every seeded discrepancy was found, nothing was "
                 "invented, and the trajectory was clean.")
        L.append("")
    return "\n".join(L)


# --- cli ----------------------------------------------------------------------

def load(traj: Path, truth_by_session: dict) -> dict[str, list[dict]]:
    if not traj.exists():
        raise SystemExit(f"{traj} does not exist -- run src/agent.py first")
    out: dict[str, list[dict]] = defaultdict(list)
    for line in traj.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        t = truth_by_session.get(rec["session_id"])
        if t is None:
            continue
        out[rec["backend"]].append(score_session(rec, t))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traj", default=str(TRAJ))
    ap.add_argument("--db", default=str(store.DB_PATH))
    ap.add_argument("--backend", action="append", default=[],
                    help="only these backends (repeatable)")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), action="append",
                    default=[], help="paired McNemar between two backends")
    ap.add_argument("--variance", nargs=2, metavar=("RUN_A", "RUN_B"),
                    help="two trajectory files from the same configuration; "
                         "reports how much it disagrees with itself")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    truth = {t["session_id"]: t for t in json.loads(TRUTH.read_text())}

    if a.variance:
        if len(a.backend) != 1:
            raise SystemExit("--variance needs exactly one --backend, so both "
                             "files are read for the same configuration")
        label = a.backend[0]
        pa, pb = (Path(x) for x in a.variance)
        ra, rb = raw_runs(pa, label), raw_runs(pb, label)
        for p, r in ((pa, ra), (pb, rb)):
            if not r:
                raise SystemExit(f"{p} holds no run for backend {label!r}")
        v = variance({k: x for k, x in ra.items() if k in truth},
                     {k: x for k, x in rb.items() if k in truth}, truth)
        report = render_variance(v, label)
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(report + "\n", encoding="utf-8")
            print(f"-> {a.out}")
        else:
            print(report)
        return
    scored = load(Path(a.traj), truth)
    if a.backend:
        scored = {k: v for k, v in scored.items() if k in a.backend}
    if not scored:
        raise SystemExit("no matching trajectories")

    by_backend = {k: aggregate(v) for k, v in sorted(scored.items())}
    comparisons = []
    for x, y in a.compare:
        if x not in scored or y not in scored:
            print(f"skipping {x} vs {y}: not both present", file=sys.stderr)
            continue
        comparisons.append(compare(scored[x], scored[y], x, y))

    report = render(by_backend, hitl_invariant(Path(a.db)), comparisons)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report + "\n", encoding="utf-8")
        print(f"-> {p}")
    else:
        print(report)


if __name__ == "__main__":
    main()
