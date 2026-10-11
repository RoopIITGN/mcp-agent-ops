"""
Tests for the routing scorer.

A scorer is the one component whose bugs are invisible in its own output: a
broken number looks exactly like a real one. So these tests are mostly about the
cases where a plausible-looking definition is wrong, and three of them exist
because the first definition WAS wrong and a run caught it.

  `test_a_refused_request_is_not_marked_wrong_for_resolving_no_date` -- scoring
  date resolution on every request marked the oracle wrong on all twelve
  out-of-scope ones for correctly refusing before resolving anything.

  `test_collecting_refusals_on_a_lucky_route_is_not_recovery` -- "refusals
  happened and the route was right" credited the fixed pipeline with eighteen
  recoveries. It never re-plans.

  `test_early_stop_alone_is_gameable` -- a supervisor that always stops after
  reconciliation scores 100% on the headline. That is why under-reach and
  fully-correct are reported beside it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import calibrate_routing  # noqa: E402
import queries as q_mod  # noqa: E402
import routing_eval as re_mod  # noqa: E402
import supervisor as sup_mod  # noqa: E402


@pytest.fixture(scope="module")
def corpus():
    path = ROOT / "data/queries.json"
    if not path.exists():
        pytest.skip("data/queries.json not generated")
    return {q["query_id"]: q for q in json.loads(path.read_text(encoding="utf-8"))}


def pick(corpus: dict, **where) -> dict:
    for q in corpus.values():
        if all(q["expect"].get(k) == v if k in q["expect"] else q.get(k) == v
               for k, v in where.items()):
            return q
    raise AssertionError(f"no query matching {where}")


def rec(q: dict, invoked, end="done", **kw) -> dict:
    base = {
        "query_id": q["query_id"], "supervisor": "test", "invoked": list(invoked),
        "invocations": [{"role": r, "refusals": 0, "stopped": "done"}
                        for r in invoked],
        "terminal_action": end, "terminal_message": "", "declined": [],
        "resolved_date": q["expect"]["session_date"], "supervisor_calls": 1,
        "worker_refusals": 0, "stopped": "terminal",
        "rejections_by_gate": {}, "recovered_roles": [],
    }
    base.update(kw)
    return base


# --- the basics ---------------------------------------------------------------

def test_the_right_route_scores_fully_correct(corpus):
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(rec(q, q["expect"]["roles"]), q)
    assert s["exact_route"] and s["decision_correct"] and s["fully_correct"]
    assert not s["over_reach"] and not s["under_reach"]
    assert s["wasted_calls"] == 0


def test_running_a_role_twice_is_over_reach_even_though_the_role_is_right(corpus):
    """Wasted work is wasted whether the role was wrong or merely repeated."""
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(rec(q, q["expect"]["roles"] + ["staging"]), q)
    assert s["over_reach"] and s["wasted_calls"] == 1
    assert not s["exact_route"]


def test_missing_a_role_is_under_reach(corpus):
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(rec(q, ["reconciliation"]), q)
    assert s["under_reach"] and not s["over_reach"]


# --- the headline, and why it is never reported alone -------------------------

def test_stopping_where_the_books_allow_scores_the_headline(corpus):
    q = pick(corpus, early_stop=True)
    s = re_mod.score_one(rec(q, q["expect"]["roles"]), q)
    assert s["early_stop_opportunity"] and s["early_stop_correct"]


def test_pressing_on_past_an_empty_book_misses_the_headline(corpus):
    q = pick(corpus, early_stop=True)
    s = re_mod.score_one(rec(q, q["expect"]["intended_roles"]), q)
    assert s["early_stop_opportunity"] and not s["early_stop_correct"]
    assert s["over_reach"]


def test_early_stop_alone_is_gameable(corpus):
    """A supervisor that always stops after reconciliation scores 100% on the
    headline, because stopping is exactly right on every early-stop request.
    Caught by under-reach and fully-correct, which is why the headline is never
    reported without them."""
    scored = [re_mod.score_one(rec(q, ["reconciliation"]), q)
              for q in corpus.values()]
    agg = re_mod.aggregate(scored)
    assert agg["early_stop_correct"] == 1.0, "the headline should be gamed here"
    assert agg["under_reach"] > 0.2, "under-reach must catch it"
    assert agg["fully_correct"] < 0.6, "fully-correct must catch it"


def test_the_two_stop_causes_stay_separate(corpus):
    causes = {re_mod.score_one(rec(q, q["expect"]["roles"]), q)["stop_cause"]
              for q in corpus.values() if q["expect"]["early_stop"]}
    assert causes == {"books_agree", "no_statement"}


# --- both sides of every pair -------------------------------------------------

def test_refusing_everything_is_caught_by_false_refusal(corpus):
    scored = [re_mod.score_one(rec(q, [], end="refuse"), q)
              for q in corpus.values()]
    agg = re_mod.aggregate(scored)
    assert agg["correct_refusal"] == 1.0
    assert agg["false_refusal"] > 0.8
    assert agg["fully_correct"] < 0.2


def test_clarifying_everything_is_caught_by_false_clarification(corpus):
    scored = [re_mod.score_one(rec(q, [], end="clarify"), q)
              for q in corpus.values()]
    agg = re_mod.aggregate(scored)
    assert agg["correct_clarification"] == 1.0
    assert agg["false_clarification"] > 0.8


# --- date resolution ----------------------------------------------------------

def test_a_refused_request_is_not_marked_wrong_for_resolving_no_date(corpus):
    """The first version scored dates on every request, which marked the oracle
    wrong on all twelve out-of-scope ones for correctly refusing before it
    resolved anything."""
    q = pick(corpus, decision="refuse")
    s = re_mod.score_one(rec(q, [], end="refuse", resolved_date=None), q)
    assert not s["date_scored"]
    agg = re_mod.aggregate([s])
    assert agg["date_n"] == 0
    assert agg["date_correct"] is None


def test_a_wrong_date_on_a_served_request_is_marked_wrong(corpus):
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(
        rec(q, q["expect"]["roles"], resolved_date="2026-01-01"), q)
    assert s["date_scored"] and not s["date_correct"]


# --- recovery, strictly -------------------------------------------------------

def test_collecting_refusals_on_a_lucky_route_is_not_recovery(corpus):
    """"Refusals happened and the route was right" credited the fixed pipeline
    with eighteen recoveries. It never re-plans; it walks a fixed route and
    collects refusals on the way."""
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(
        rec(q, q["expect"]["roles"], worker_refusals=3, recovered_roles=[]), q)
    assert not s["recovered"]


def test_a_refused_role_that_later_succeeds_is_recovery(corpus):
    q = pick(corpus, intent="premature_stage")
    s = re_mod.score_one(
        rec(q, ["staging", "reconciliation", "triage", "staging"],
            worker_refusals=1, recovered_roles=["staging"]), q)
    assert s["recovered"]


def test_a_relayed_refusal_is_not_a_plan_caused_one(corpus):
    """A worker refusal is a success when the client asked about a date with no
    statement, and a failure when the plan skipped a precondition. Ground
    truth's stop_cause separates them rather than a heuristic."""
    q = pick(corpus, intent="date_with_no_data")
    s = re_mod.score_one(rec(q, ["reconciliation"], worker_refusals=3), q)
    assert s["refusal_relayed"]
    assert s["plan_caused_refusals"] == 0
    assert not s["recovered"]


def test_a_refusal_outside_that_class_is_plan_caused(corpus):
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(rec(q, q["expect"]["roles"], worker_refusals=2), q)
    assert s["plan_caused_refusals"] == 2
    assert not s["refusal_relayed"]


# --- declines -----------------------------------------------------------------

def test_a_partial_decline_must_name_what_it_declined(corpus):
    q = pick(corpus, intent="cross_account")
    other = q["expect"]["declines"][0]
    good = re_mod.score_one(
        rec(q, q["expect"]["roles"], declined=[other]), q)
    bad = re_mod.score_one(rec(q, q["expect"]["roles"]), q)
    assert good["decline_correct"] and not bad["decline_correct"]
    assert good["fully_correct"] and not bad["fully_correct"]


def test_naming_it_in_the_message_counts(corpus):
    """A decline stated to the client in words is a decline."""
    q = pick(corpus, intent="cross_account")
    other = q["expect"]["declines"][0]
    s = re_mod.score_one(
        rec(q, q["expect"]["roles"],
            terminal_message=f"Done. I cannot access {other} from this request."),
        q)
    assert s["decline_correct"]


# --- gates --------------------------------------------------------------------

def test_a_binding_rejection_is_an_isolation_violation(corpus):
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(
        rec(q, [], end=None, stopped="supervisor_cap",
            rejections_by_gate={"account_binding": 10}), q)
    assert s["isolation_violation"]
    assert re_mod.aggregate([s])["isolation_violations"] == 1


def test_a_validity_rejection_is_not_an_isolation_violation(corpus):
    """The two must stay separate: one is a schema failure, the other an
    isolation failure, and a report showing them identically hides the more
    serious."""
    q = pick(corpus, intent="full_reconcile", early_stop=False)
    s = re_mod.score_one(
        rec(q, [], end=None, stopped="supervisor_cap",
            rejections_by_gate={"decision_invalid": 10}), q)
    assert not s["isolation_violation"]
    assert re_mod.aggregate([s])["rejections_by_gate"] == {"decision_invalid": 10}


# --- head to head -------------------------------------------------------------

def test_mcnemar_on_a_clean_split_is_significant():
    """Fifteen to nothing on the early-stop class."""
    assert re_mod.mcnemar_exact(15, 0) < 0.01
    assert re_mod.mcnemar_exact(0, 0) == 1.0
    assert re_mod.mcnemar_exact(5, 5) == 1.0


def test_compare_pairs_only_shared_requests(corpus):
    a = [re_mod.score_one(rec(q, q["expect"]["roles"]), q)
         for q in list(corpus.values())[:10]]
    b = [re_mod.score_one(rec(q, q["expect"]["intended_roles"] or []), q)
         for q in list(corpus.values())[:6]]
    h = re_mod.compare(a, b, "a", "b")
    assert h["paired"] == 6


# --- calibration --------------------------------------------------------------

def test_every_stub_has_expectations():
    """A stub nobody asserts anything about is a stub that proves nothing."""
    assert set(calibrate_routing.EXPECTED) == set(sup_mod.POLICIES)


def test_every_metric_has_a_stub_that_moves_it():
    """A metric with nothing producing it reads zero forever, and "no
    supervisor did this" becomes indistinguishable from "the scorer cannot see
    this"."""
    claims = " ".join(name for _, cs in calibrate_routing.EXPECTED.values()
                      for name, _ in cs)
    for word in ("over-reach", "under-reach", "early-stop", "refusal",
                 "clarif", "course correct", "cap", "isolation"):
        assert word in claims.lower(), word


def test_the_report_shows_both_sides_of_each_pair(corpus):
    scored = [re_mod.score_one(rec(q, q["expect"]["roles"]), q)
              for q in corpus.values()]
    text = re_mod.render({"test": re_mod.aggregate(scored)},
                         {"test": re_mod.by_intent(scored)}, None)
    for header in ("false refuse", "false clarify", "over", "under",
                   "retried ok", "corrected", "plan-caused", "relayed"):
        assert header in text, header
