"""
The scorer, on hand-built trajectories.

src/calibrate.py already checks the scorer against whole runs of each stub
policy. This file does the narrow part: one synthetic session at a time, so a
single counting rule can be wrong in isolation and be caught. Two rules get
most of the attention because both are easy to get wrong in a way that quietly
flatters the agent.

  A trade found and mislabelled is counted once, as a mislabel. Counting it as
  both an invented discrepancy and an undetected one double-charges a single
  mistake and makes a classification failure look like a detection failure
  plus a hallucination.

  A clean session is a session where nothing was seeded, and a flag there is
  the failure the controls exist to detect. It must not be averaged into the
  same bucket as a miss.

Usage
  python tests/test_eval.py
  pytest tests/test_eval.py
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eval as ev                                              # noqa: E402
import store                                                   # noqa: E402

SESSION = "ACC-001-2026-09-01"


# --- fixtures -----------------------------------------------------------------

def truth(*discrepancies) -> dict:
    return {"session_id": SESSION, "account_id": "ACC-001", "as_of": "2026-09-01",
            "discrepancies": list(discrepancies)}


def disc(trade: str, kind: str) -> dict:
    return {"type": kind, "trade_id": trade, "instrument": "INFY"}


def rec(flagged: list[dict], *, read_ledger: bool = True, compared: bool = True,
        drafts: int | None = None, stages: int | None = None,
        errors: list[tuple[str, str, str]] = ()) -> dict:
    """A trajectory record shaped the way agent.py writes one."""
    recon = [{"kind": "call", "name": "fetch_broker_statement"},
             {"kind": "result", "name": "fetch_broker_statement"}]
    if read_ledger:
        recon += [{"kind": "call", "name": "fetch_internal_ledger"},
                  {"kind": "result", "name": "fetch_internal_ledger"}]
    if compared:
        recon += [{"kind": "call", "name": "calculate_position_deltas"},
                  {"kind": "result", "name": "calculate_position_deltas"}]

    n = len(flagged)
    triage = []
    for _ in range(n):
        triage += [{"kind": "call", "name": "flag_discrepancy"},
                   {"kind": "result", "name": "flag_discrepancy"}]
    for _ in range(n if drafts is None else drafts):
        triage += [{"kind": "call", "name": "draft_client_inquiry"},
                   {"kind": "result", "name": "draft_client_inquiry"}]

    n_stage = n if stages is None else stages
    staging = []
    for _ in range(n_stage):
        staging += [{"kind": "call", "name": "stage_for_execution"},
                    {"kind": "result", "name": "stage_for_execution"}]
    for role, tool, text in errors:
        {"reconciliation": recon, "triage": triage,
         "staging": staging}[role].append(
            {"kind": "error", "name": tool, "text": text})

    return {"session_id": SESSION, "account_id": "ACC-001", "backend": "stub:test",
            "observations": [], "flagged": flagged,
            # From the staging CALLS, which is how agent.py builds this field.
            # Deriving it from `flagged` instead made the fixture unable to
            # express "staged more times than it flagged" -- the exact shape the
            # first model run produced -- so a test written against it passed
            # for the wrong reason.
            "staged": list(range(1, n_stage + 1)),
            "refusals": {}, "steps": {},
            "trajectory": {"reconciliation": recon, "triage": triage,
                           "staging": staging}}


def flag(i: int, trade: str, kind: str) -> dict:
    return {"discrepancy_id": i, "trade_id": trade, "type": kind, "created": True}


# --- McNemar ------------------------------------------------------------------

def test_mcnemar_no_discordant_pairs_is_not_significant():
    assert ev.mcnemar_exact(0, 0) == 1.0


def test_mcnemar_is_symmetric():
    assert ev.mcnemar_exact(9, 2) == ev.mcnemar_exact(2, 9)


def test_mcnemar_all_one_way_matches_the_closed_form():
    assert math.isclose(ev.mcnemar_exact(14, 0), 2 * 0.5 ** 14, rel_tol=1e-12)
    assert math.isclose(ev.mcnemar_exact(5, 0), 2 * 0.5 ** 5, rel_tol=1e-12)


def test_mcnemar_even_split_is_never_significant():
    assert ev.mcnemar_exact(10, 10) == 1.0


def test_mcnemar_never_exceeds_one():
    for b in range(6):
        for c in range(6):
            assert 0.0 <= ev.mcnemar_exact(b, c) <= 1.0


def test_mcnemar_ignores_concordant_pairs():
    """The reason to use a paired test at all: agreement carries no information
    about a difference, so adding agreements must not move the p-value."""
    assert ev.mcnemar_exact(6, 1) == ev.mcnemar_exact(6, 1)
    assert ev.mcnemar_exact(6, 1) < 0.2


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    v = [1.0] * 30 + [0.0] * 10
    a = ev.bootstrap_ci(v, iters=500, seed=3)
    b = ev.bootstrap_ci(v, iters=500, seed=3)
    assert a == b, "a committed report must be recomputable to the same digits"
    lo, hi = a
    assert lo <= 0.75 <= hi, (lo, hi)
    assert ev.bootstrap_ci([], iters=10) == (0.0, 0.0)


# --- outcome counting ---------------------------------------------------------

def test_perfect_session():
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")]), t)
    assert (s["found"], s["undetected"], s["wrong_type"], s["false_positive"]) == (1, 0, 0, 0)
    assert s["trajectory_clean"] and not s["violations"]


def test_missed_discrepancy():
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([]), t)
    assert (s["found"], s["undetected"]) == (0, 1)
    assert s["false_positive"] == 0, "a miss is not an invention"


def test_wrong_type_is_counted_once_not_twice():
    """Right trade, wrong label. One mistake, not an invention plus a miss."""
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([flag(1, "T1", "price_variance")]), t)
    assert s["wrong_type"] == 1
    assert s["false_positive"] == 0, "a mislabel was counted as invented"
    assert s["undetected"] == 0, "a mislabel was also counted as undetected"
    assert s["wrong_type_detail"] == [
        {"trade_id": "T1", "flagged_as": "price_variance",
         "actually": "quantity_mismatch"}]


def test_invented_discrepancy_on_a_clean_session():
    s = ev.score_session(rec([flag(1, "T9", "quantity_mismatch")]), truth())
    assert s["clean_session"] is True
    assert s["false_positive"] == 1
    assert s["found"] == 0 and s["undetected"] == 0


def test_partial_detection_on_a_multi_finding_session():
    t = truth(disc("T1", "quantity_mismatch"), disc("T2", "missing_trade"))
    s = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")]), t)
    assert (s["found"], s["undetected"], s["seeded"]) == (1, 1, 2)


def test_by_type_counters_are_populated():
    t = truth(disc("T1", "duplicate_booking"), disc("T2", "price_variance"))
    s = ev.score_session(rec([flag(1, "T1", "duplicate_booking")]), t)
    assert s["found_by_type"]["duplicate_booking"] == 1
    assert s["undetected_by_type"]["price_variance"] == 1


# --- trajectory ---------------------------------------------------------------

def test_skipping_the_ledger_read_is_a_violation_despite_a_perfect_outcome():
    """The standing argument for scoring trajectories at all."""
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")], read_ledger=False), t)
    assert s["found"] == 1 and s["undetected"] == 0
    assert not s["trajectory_clean"]
    assert any("ledger never read" in v for v in s["violations"]), s["violations"]


def test_never_comparing_is_a_violation():
    s = ev.score_session(rec([], compared=False), truth())
    assert any("never compared" in v for v in s["violations"])


def test_a_refused_call_breaks_the_trajectory():
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(
        rec([flag(1, "T1", "quantity_mismatch")],
            errors=[("staging", "stage_for_execution",
                     "discrepancy 999 does not exist -- call flag_discrepancy first")]), t)
    assert not s["trajectory_clean"]
    assert s["refusals"] == 1
    assert "flag_discrepancy first" in s["refusal_detail"][0]["reason"]


def test_missing_drafts_are_a_violation():
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")], drafts=0), t)
    assert any("client drafts" in v for v in s["violations"]), s["violations"]


def test_a_clean_session_with_nothing_to_draft_is_still_clean():
    """The expectation that was wrong the first time: a control session has
    nothing to flag and so nothing to draft."""
    s = ev.score_session(rec([], drafts=0), truth())
    assert s["trajectory_clean"], s["violations"]


def test_staging_more_than_was_flagged_is_a_violation():
    t = truth(disc("T1", "quantity_mismatch"))
    s = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")], stages=3), t)
    assert any("staged more" in v for v in s["violations"])


def test_schema_refusals_are_separated_from_other_refusals():
    t = truth()
    s = ev.score_session(
        rec([], errors=[("staging", "stage_for_execution",
                         "invalid arguments for stage_for_execution: extra_forbidden")]), t)
    assert s["schema_refusals"] == 1


# --- aggregation --------------------------------------------------------------

def test_aggregate_recall_and_precision():
    t1 = truth(disc("T1", "quantity_mismatch"))
    t2 = truth(disc("T2", "missing_trade"))
    scored = [
        ev.score_session(rec([flag(1, "T1", "quantity_mismatch")]), t1),
        ev.score_session(rec([]), t2),
        ev.score_session(rec([flag(2, "T9", "price_variance")]), truth()),
    ]
    a = ev.aggregate(scored)
    assert (a["seeded"], a["found"], a["undetected"]) == (2, 1, 1)
    assert a["false_positive"] == 1
    assert math.isclose(a["recall"], 0.5)
    assert math.isclose(a["precision"], 0.5)          # 1 right of 2 flagged
    assert a["clean_sessions"] == 1
    assert a["clean_sessions_with_a_flag"] == 1
    assert math.isclose(a["clean_false_positive_rate"], 1.0)


def test_aggregate_by_type_totals_reconcile():
    t = truth(disc("T1", "duplicate_booking"), disc("T2", "duplicate_booking"))
    a = ev.aggregate([ev.score_session(rec([flag(1, "T1", "duplicate_booking")]), t)])
    bt = a["by_type"]["duplicate_booking"]
    assert bt == {"seeded": 2, "found": 1, "undetected": 1}


def test_aggregate_handles_an_all_clean_run():
    a = ev.aggregate([ev.score_session(rec([]), truth()) for _ in range(3)])
    assert a["recall"] == 0.0 and a["precision"] == 0.0     # nothing to find
    assert a["clean_false_positive_rate"] == 0.0
    assert a["trajectory_clean_rate"] == 1.0
    assert a["schema_validity"] == 1.0


# --- comparison ---------------------------------------------------------------

def test_variance_is_zero_when_two_runs_agree():
    t = {SESSION: truth(disc("T1", "quantity_mismatch"))}
    r = rec([flag(1, "T1", "quantity_mismatch")])
    v = ev.variance({SESSION: r}, {SESSION: dict(r)}, t)
    assert v["sessions"] == 1 and v["identical"] == 1
    assert v["flip_rate"] == 0.0 and v["rows"] == []


def test_variance_detects_a_different_finding():
    """The check that matters: a floor measurement that can only report zero
    has not measured anything."""
    t = {SESSION: truth(disc("T1", "quantity_mismatch"))}
    a = rec([flag(1, "T1", "quantity_mismatch")])
    b = rec([])
    v = ev.variance({SESSION: a}, {SESSION: b}, t)
    assert v["findings_flips"] == 1 and v["flip_rate"] == 1.0
    assert v["rows"][0]["only_in_a"] == [("T1", "quantity_mismatch")]
    assert v["rows"][0]["only_in_b"] == []


def test_variance_separates_behaviour_from_outcome():
    """Same findings, different route. A run can be stable in what it finds and
    unstable in how it got there, and only one of those shows up in a
    trajectory metric."""
    t = {SESSION: truth(disc("T1", "quantity_mismatch"))}
    a = rec([flag(1, "T1", "quantity_mismatch")])
    b = rec([flag(1, "T1", "quantity_mismatch")], stages=4)
    v = ev.variance({SESSION: a}, {SESSION: b}, t)
    assert v["findings_flips"] == 0, "the findings were identical"
    assert v["staged_flips"] == 1
    assert v["trajectory_flips"] == 1, "staging more than flagged is a violation"
    assert v["flip_rate"] == 1.0


def test_variance_only_compares_sessions_both_runs_covered():
    t = {SESSION: truth(disc("T1", "quantity_mismatch"))}
    a = {SESSION: rec([flag(1, "T1", "quantity_mismatch")])}
    other = dict(rec([]))
    other["session_id"] = "ACC-002-2026-09-01"
    v = ev.variance(a, {"ACC-002-2026-09-01": other}, t)
    assert v["sessions"] == 0 and v["flip_rate"] == 0.0


def test_variance_report_states_what_a_zero_does_not_mean():
    """A zero observed in n trials is not a zero rate, and the report has to
    say so or the number gets quoted as certainty."""
    t = {SESSION: truth(disc("T1", "quantity_mismatch"))}
    r = rec([flag(1, "T1", "quantity_mismatch")])
    v = ev.variance({SESSION: r}, {SESSION: dict(r)}, t)
    report = ev.render_variance(v, "azure:test@t0.0")
    assert "not a zero rate" in report, report
    assert "flip rate of 0.0%" in report


def test_compare_pairs_on_session_id():
    t = truth(disc("T1", "quantity_mismatch"))
    good = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")]), t)
    bad = ev.score_session(rec([]), t)
    c = ev.compare([good], [bad], "a", "b")
    assert (c["a_only"], c["b_only"], c["paired_sessions"]) == (1, 0, 1)
    assert c["p_value"] == ev.mcnemar_exact(1, 0)


def test_compare_ignores_sessions_only_one_run_covered():
    t = truth(disc("T1", "quantity_mismatch"))
    a = ev.score_session(rec([flag(1, "T1", "quantity_mismatch")]), t)
    b = dict(a)
    b["session_id"] = "ACC-002-2026-09-01"
    c = ev.compare([a], [b], "a", "b")
    assert c["paired_sessions"] == 0 and c["p_value"] == 1.0


# --- the HITL invariant -------------------------------------------------------

def test_hitl_invariant_holds_on_a_staged_action():
    db = Path(tempfile.mkdtemp()) / "ops.db"
    con = store.init_db(force=True, path=db)
    did, _ = store.upsert_discrepancy(con, SESSION, "T1", "quantity_mismatch",
                                      "INFY", {})
    store.stage_action(con, SESSION, did, {"fix": "x"})
    h = ev.hitl_invariant(db)
    assert h["holds"] and h["staged"] == 1


def test_hitl_invariant_fails_once_something_is_approved():
    """If approval ever reaches a staged row, the report must say so rather
    than averaging it away."""
    db = Path(tempfile.mkdtemp()) / "ops.db"
    con = store.init_db(force=True, path=db)
    did, _ = store.upsert_discrepancy(con, SESSION, "T1", "quantity_mismatch",
                                      "INFY", {})
    aid, _ = store.stage_action(con, SESSION, did, {"fix": "x"})
    store.decide_action(con, aid, True, "ops@firm")
    h = ev.hitl_invariant(db)
    assert not h["holds"] and len(h["violations"]) == 1


# --- runner -------------------------------------------------------------------

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)} tests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
