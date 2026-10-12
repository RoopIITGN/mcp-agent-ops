"""
Tests for the client request corpus.

Several of these are not ordinary unit tests but assertions that the
measurement is sound, and they are the reason the file exists:

  the corpus is byte-reproducible from its seed, so a routing number can be
  recomputed later against the same inputs;

  no ground truth reaches the model, because an orchestrator shown its own
  answer key scores perfectly and measures nothing;

  the early-stop class is populated, because it is the only class that
  separates deciding-once from deciding-each-step, and an earlier version of
  the generator left it with one member by accident;

  every query names a session and a trade that exist, because a query about a
  missing session would be refused for the wrong reason and the refusal would
  be scored as judgement.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import queries  # noqa: E402


@pytest.fixture(scope="module")
def corpus():
    return queries.build()


@pytest.fixture(scope="module")
def sessions():
    return json.loads((ROOT / "data/sessions.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def truth():
    return {t["session_id"]: t for t in json.loads(
        (ROOT / "data/seeded_truth.json").read_text(encoding="utf-8"))}


# --- reproducibility ----------------------------------------------------------

def test_same_seed_is_byte_identical():
    a = json.dumps(queries.build(seed=123), sort_keys=True)
    b = json.dumps(queries.build(seed=123), sort_keys=True)
    assert a == b


def test_different_seed_changes_the_corpus():
    a = json.dumps(queries.build(seed=1), sort_keys=True)
    b = json.dumps(queries.build(seed=2), sort_keys=True)
    assert a != b


@pytest.mark.parametrize("seed", [1, 2, 7, 42, 999, 20261011])
def test_corpus_builds_cleanly_at_many_seeds(seed):
    """Not redundant with the default-seed tests. The duplicate-text assertion
    inside build() first fired at seed 2: two clarify queries landed on one
    account and one template, and their text was byte-identical. At the default
    seed that collision simply had not happened. A generator that is only
    correct for one seed is correct by luck."""
    corpus = queries.build(seed)
    assert len({q["text"] for q in corpus}) == len(corpus)
    assert len({q["query_id"] for q in corpus}) == len(corpus)


@pytest.mark.parametrize("seed", [1, 2, 7, 42])
def test_clarify_queries_never_collide_at_any_seed(seed):
    """Their templates embed no date, so client plus template is all that
    distinguishes them."""
    corpus = queries.build(seed)
    for intent in ("underspecified_date", "ambiguous_date"):
        qs = [q for q in corpus if q["intent"] == intent]
        assert len({q["text"] for q in qs}) == len(qs), intent
        assert len({q["client_id"] for q in qs}) == len(qs), intent


def test_committed_file_matches_the_generator():
    """data/queries.json is what the eval reads. If it has drifted from the
    generator, every routing number was computed against something no longer
    reproducible."""
    path = ROOT / "data/queries.json"
    if not path.exists():
        pytest.skip("data/queries.json not generated")
    assert json.loads(path.read_text(encoding="utf-8")) == queries.build()


# --- shape --------------------------------------------------------------------

def test_counts_match_the_intent_table(corpus):
    expected = sum(s["n"] for s in queries.INTENTS.values())
    assert len(corpus) == expected
    by_intent: dict[str, int] = {}
    for q in corpus:
        by_intent[q["intent"]] = by_intent.get(q["intent"], 0) + 1
    assert set(by_intent) == set(queries.INTENTS)
    for intent, spec in queries.INTENTS.items():
        assert by_intent[intent] == spec["n"], intent


def test_every_intent_has_at_least_six_queries(corpus):
    """Unbalanced on purpose -- the two intents that can truncate carry more --
    but nothing drops below a usable sample."""
    for spec in queries.INTENTS.values():
        assert spec["n"] >= 6


def test_query_ids_and_text_are_unique(corpus):
    assert len({q["query_id"] for q in corpus}) == len(corpus)
    assert len({q["text"] for q in corpus}) == len(corpus)


def test_every_intent_has_several_templates():
    """N queries of one intent must not be N copies of one sentence, or the
    orchestrator can route on a memorised string instead of the request."""
    for intent, tmpls in queries.TEMPLATES.items():
        assert len(tmpls) >= 4, intent


def test_intents_do_not_all_draw_the_same_sessions(corpus):
    """The bug this is here for: the first generator's session picker was
    `pool[(i * 7 + seed) % len(pool)]`, which does not depend on the intent at
    all, so every intent drawing from the full pool picked identical sessions."""
    picks: dict[str, set[str]] = {}
    for q in corpus:
        if q["session_id"]:
            picks.setdefault(q["intent"], set()).add(q["session_id"])
    seen = list(picks.values())
    for i in range(len(seen)):
        for j in range(i + 1, len(seen)):
            assert seen[i] != seen[j], "two intents drew identical session sets"


# --- routing truth ------------------------------------------------------------

def test_intended_roles_match_the_intent_table(corpus):
    for q in corpus:
        spec = queries.INTENTS[q["intent"]]
        assert q["expect"]["decision"] == spec["decision"]
        assert q["expect"]["intended_roles"] == list(spec["intended"])


def test_correct_route_is_a_prefix_of_what_was_asked(corpus):
    """Data can only truncate a route, never extend it. A correct route longer
    than the request would mean the orchestrator is expected to do work nobody
    asked for."""
    for q in corpus:
        e = q["expect"]
        assert e["roles"] == e["intended_roles"][:len(e["roles"])], q["query_id"]


def test_roles_are_a_subsequence_of_canonical(corpus):
    canon = list(queries.CANONICAL)
    for q in corpus:
        for key in ("roles", "intended_roles"):
            roles = q["expect"][key]
            assert [r for r in canon if r in roles] == roles, q["query_id"]
            assert len(set(roles)) == len(roles), q["query_id"]


def test_refuse_and_clarify_route_nowhere(corpus):
    for q in corpus:
        if q["expect"]["decision"] in ("refuse", "clarify"):
            assert q["expect"]["roles"] == []
            assert q["expect"]["intended_roles"] == []


def test_route_depth_follows_the_books(corpus, truth):
    """Ground truth has to agree with the data it was derived from: a session
    with findings gets the full requested route, a session whose books agree
    gets truncated to reconciliation alone."""
    for q in corpus:
        sid = q["session_id"]
        if not sid or not q["expect"]["intended_roles"]:
            continue
        has_findings = bool(truth[sid]["discrepancies"])
        roles = q["expect"]["roles"]
        if has_findings:
            assert roles == q["expect"]["intended_roles"], q["query_id"]
        else:
            assert roles == ["reconciliation"], q["query_id"]


def test_early_stop_flag_is_consistent(corpus):
    for q in corpus:
        e = q["expect"]
        expected = bool(e["intended_roles"]) and e["roles"] != e["intended_roles"]
        assert e["early_stop"] is expected, q["query_id"]


# --- the class that makes the comparison possible -----------------------------

def test_early_stop_class_is_well_populated(corpus):
    """The only class that separates deciding-once from deciding-each-step. An
    earlier generator left it with a single member, which would have reduced
    the headline comparison to intent recognition."""
    early = [q for q in corpus if q["expect"]["early_stop"]]
    assert len(early) >= 8, f"only {len(early)} early-stop queries"
    intents = {q["intent"] for q in early}
    assert len(intents) >= 2, f"early-stop confined to one intent: {intents}"


def test_early_stop_queries_really_do_ask_for_more(corpus):
    for q in corpus:
        if q["expect"]["early_stop"]:
            assert len(q["expect"]["intended_roles"]) > len(q["expect"]["roles"])
            assert q["expect"]["roles"] == ["reconciliation"], q["query_id"]


def test_the_three_ceilings_are_strictly_ordered(corpus):
    """The whole argument for the corpus in one assertion. If these were equal,
    the corpus would not distinguish the three strategies and there would be
    nothing to measure."""
    c = queries.ceilings(corpus)
    assert c["fixed"] < c["plan_once"] < c["iterate"] == c["total"]
    assert c["fixed"] / c["total"] <= 0.35, (
        "the fixed pipeline scores too well -- the corpus is not varied enough")
    assert c["plan_once"] == c["total"] - c["early_stop"]


def test_plan_once_ceiling_is_below_one(corpus):
    """There must be queries no amount of upfront reasoning can get right,
    otherwise re-planning has nothing to earn."""
    c = queries.ceilings(corpus)
    assert c["plan_once"] < c["total"]


# --- the measurement is not rigged --------------------------------------------

def test_prompt_view_carries_no_ground_truth(corpus):
    forbidden = {"intent", "expect", "session_id"}
    for q in corpus:
        view = queries.prompt_view(q)
        assert set(view) == set(queries.PROMPT_FIELDS)
        assert not (set(view) & forbidden), q["query_id"]


def test_prompt_view_text_never_names_a_role_or_a_tool(corpus):
    """A query saying "run the reconciliation agent then the triage agent"
    would be dictating the route, and routing accuracy would be measuring
    instruction-following. "reconciliation" as ordinary English is what a client
    would write, so what is banned is the role-and-tool vocabulary."""
    import tools
    banned = set(tools.TOOLS) | {"triage", "staging agent",
                                 "reconciliation agent", "triage agent"}
    for q in corpus:
        low = queries.prompt_view(q)["text"].lower()
        for word in banned:
            assert word.lower() not in low, f"{q['query_id']} names {word}"


def test_no_single_intent_owns_the_full_route(corpus):
    """If every 'all three roles' query came from one intent, an orchestrator
    could score well by recognising that intent alone."""
    canon = list(queries.CANONICAL)
    intents = {q["intent"] for q in corpus if q["expect"]["roles"] == canon}
    assert len(intents) >= 3, intents


# --- the corpus refers to things that exist -----------------------------------

def test_sessions_named_by_queries_exist(corpus, sessions):
    known = {s["session_id"] for s in sessions}
    for q in corpus:
        if q["session_id"] is not None:
            assert q["session_id"] in known, q["query_id"]


def test_client_id_matches_the_session(corpus):
    for q in corpus:
        if q["session_id"]:
            assert q["session_id"].startswith(q["client_id"]), q["query_id"]


def test_premature_stage_names_a_real_broken_trade(corpus, truth):
    """This intent asks for a correction by trade id, so the trade has to be
    genuinely broken or the query tests nothing."""
    found = 0
    for q in corpus:
        if q["intent"] != "premature_stage":
            continue
        found += 1
        ds = truth[q["session_id"]]["discrepancies"]
        assert ds, q["query_id"]
        assert any(d["trade_id"] in q["text"] for d in ds), q["query_id"]
        assert not q["expect"]["early_stop"], (
            f"{q['query_id']} cannot early-stop: it names a real break")
    assert found == queries.INTENTS["premature_stage"]["n"]


def test_ambiguous_queries_carry_no_date(corpus):
    for q in corpus:
        if q["intent"] != "ambiguous_date":
            continue
        assert q["session_id"] is None, q["query_id"]
        text = q["text"]
        assert not re.search(r"\d{4}-\d{2}-\d{2}", text), q["query_id"]
        assert not re.search(r"\b(Sep|September|Jan|January)\b", text), q["query_id"]
        assert not re.search(r"\b\d{1,2}(st|nd|rd|th)\b", text), q["query_id"]


def test_cross_account_names_a_second_real_account(corpus, sessions):
    known = {s["account_id"] for s in sessions}
    found = 0
    for q in corpus:
        if q["intent"] != "cross_account":
            continue
        found += 1
        declines = q["expect"]["declines"]
        assert len(declines) == 1, q["query_id"]
        other = declines[0]
        assert other in known and other != q["client_id"], q["query_id"]
        assert other in q["text"], q["query_id"]
    assert found == queries.INTENTS["cross_account"]["n"]


def test_read_only_intents_include_clean_sessions(corpus, truth):
    """'The two books agree' is an answer the system must be able to give. If
    every read-only query landed on a broken session, that path would never be
    exercised by a query."""
    for intent in ("compare_only", "notes_check", "rerun_compare"):
        sids = [q["session_id"] for q in corpus if q["intent"] == intent]
        assert any(not truth[s]["discrepancies"] for s in sids), intent


# --- the data gap, asserted rather than assumed -------------------------------

def test_depth_two_is_still_unreachable(sessions, truth):
    """The corpus has no within-tolerance price noise, so no session has
    observations without reportable findings. Ground truth depends on that, so
    it is asserted here: the day a near-miss case is added this fails loudly
    instead of the truth going quietly wrong."""
    tol = {a["account_id"]: a["tolerance"] for a in json.loads(
        (ROOT / "data/accounts.json").read_text(encoding="utf-8"))}
    depths = {queries._depth(s, tol[s["account_id"]],
                             len(truth[s["session_id"]]["discrepancies"]))
              for s in sessions}
    assert depths == {1, 3}, (
        f"depths present: {sorted(depths)} -- depth 2 has become reachable, so "
        f"CLEAN_QUOTA and the route-depth tests need revisiting")


def test_summary_reports_all_three_ceilings(corpus):
    s = queries.summarise(corpus)
    for word in ("fixed", "plan_once", "iterate", "early-stop"):
        assert word in s


# --- dates: a clock, not a calendar ------------------------------------------
#
# An earlier draft gave the supervisor each client's loaded statement dates. That
# made the orchestrator a validator of what exists in the books, which is a
# worker's job. These tests pin the replacement: one global business date, and a
# request naming a date with no data goes to a worker rather than being refused
# upfront.

def test_no_per_client_calendar_reaches_the_prompt(corpus):
    """The supervisor must not be able to enumerate a client's loaded periods."""
    assert "available_dates" not in queries.PROMPT_FIELDS
    for q in corpus:
        assert "available_dates" not in q, q["query_id"]
        assert set(queries.prompt_view(q)) == set(queries.PROMPT_FIELDS)


def test_business_date_is_after_every_session(corpus, sessions):
    """So "the 8th" and "8 Sep" resolve into the past rather than the future."""
    latest = max(s["as_of"] for s in sessions)
    assert queries.BUSINESS_DATE > latest


def test_business_date_agrees_with_the_prompt_builder():
    import plan
    assert plan.BUSINESS_DATE == queries.BUSINESS_DATE


def test_every_query_has_a_resolvable_date_target(corpus):
    """`expect.session_date` is what the supervisor should emit. None only where
    the request names no date at all, which is the clarify class."""
    for q in corpus:
        target = q["expect"]["session_date"]
        if q["expect"]["decision"] == "clarify":
            assert target is None, q["query_id"]
        else:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", target), q["query_id"]


def test_resolved_date_matches_the_session_when_one_exists(corpus):
    for q in corpus:
        if q["session_id"]:
            assert q["session_id"].endswith(q["expect"]["session_date"])


# --- a date with no statement is a worker's finding, not a supervisor's -------

def test_absent_dates_really_have_no_session(corpus, sessions):
    known = {s["session_id"] for s in sessions}
    found = 0
    for q in corpus:
        if q["intent"] != "date_with_no_data":
            continue
        found += 1
        target = q["expect"]["session_date"]
        assert f"{q['client_id']}-{target}" not in known, q["query_id"]
        assert q["session_id"] is None, q["query_id"]
    assert found == queries.INTENTS["date_with_no_data"]["n"]


def test_no_data_queries_are_run_not_refused(corpus):
    """The point of the class. Refusing upfront would mean the orchestrator had
    decided what exists in the books without reading them."""
    for q in corpus:
        if q["intent"] == "date_with_no_data":
            assert q["expect"]["decision"] == "run", q["query_id"]
            assert q["expect"]["roles"] == ["reconciliation"], q["query_id"]
            assert q["expect"]["stop_cause"] == "no_statement", q["query_id"]


def test_the_two_stop_causes_are_reported_separately(corpus):
    """Books that agree and a date with no statement both truncate the route,
    but they are different findings and conflating them would hide one."""
    causes = {q["expect"]["stop_cause"] for q in corpus
              if q["expect"]["early_stop"]}
    assert causes == {"books_agree", "no_statement"}


def test_stop_cause_is_set_exactly_when_the_route_truncates(corpus):
    for q in corpus:
        e = q["expect"]
        assert (e["stop_cause"] is not None) == e["early_stop"], q["query_id"]


def test_absent_dates_are_well_formed_but_unloaded(corpus):
    """Well-formed matters: a malformed date would be refused for the wrong
    reason and the refusal would be scored as judgement."""
    for d in queries.ABSENT_DATES:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)


# --- two kinds of unclear date, measured separately ---------------------------
#
# The design rule: the supervisor may fill in a missing part of a date only when
# the business date leaves exactly one plausible reading. Inferring a year from
# "8 Sep" is one reading. Inferring a month from "the 8th" is two -- this month,
# whose 8th has passed, or last month's -- so it asks.
#
# Splitting this from `ambiguous_date` is not bookkeeping. Asking when no date is
# given is easy; asking when a DAY is given is the harder call, because a valid
# date is one inference away. Silently resolving is the dangerous failure, since
# the client acts on the report, so it gets its own metric.

def test_resolvable_date_forms_all_name_a_month():
    """Each form leaves only the year missing, and the business date settles
    that. A form missing the month would make the ground truth wrong."""
    for form in queries.DATE_FORMS:
        assert "{month}" in form or "{mon}" in form or "{iso}" in form, form


def test_day_only_phrasing_is_absent_from_resolvable_forms():
    for form in queries.DATE_FORMS:
        assert "{suffix}" not in form, form


def test_underspecified_queries_name_a_day_and_no_month(corpus):
    found = 0
    for q in corpus:
        if q["intent"] != "underspecified_date":
            continue
        found += 1
        text = q["text"]
        assert re.search(r"\bthe \d{1,2}(st|nd|rd|th)\b", text), q["query_id"]
        for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                  "Sep", "Oct", "Nov", "Dec", "2026", "2025"):
            assert m not in text, f"{q['query_id']} leaked {m}"
    assert found == queries.INTENTS["underspecified_date"]["n"]


def test_underspecified_days_would_resolve_to_a_real_session(corpus, sessions):
    """Adversarial on purpose: every day named is a real statement day, so
    resolving it against the current month yields a valid-looking session. If the
    days were nonsense, guessing would fail on its own and the class would not
    be testing judgement."""
    real_days = {int(s["as_of"].split("-")[2]) for s in sessions}
    for phrase in queries.DAY_ONLY:
        day = int(re.search(r"\d+", phrase).group(0))
        assert day in real_days, phrase


def test_both_clarify_classes_route_nowhere_and_resolve_no_date(corpus):
    for q in corpus:
        if q["expect"]["decision"] != "clarify":
            continue
        assert q["expect"]["roles"] == [], q["query_id"]
        assert q["expect"]["session_date"] is None, q["query_id"]
        assert q["session_id"] is None, q["query_id"]


def test_clarify_classes_say_what_the_question_is_about(corpus):
    """So a clarification can be scored for being the RIGHT question, not just
    for being a question."""
    about = {q["intent"]: q["expect"]["clarify_about"] for q in corpus
             if q["expect"]["decision"] == "clarify"}
    assert about == {"underspecified_date": "month", "ambiguous_date": "date"}


def test_acting_classes_carry_no_clarify_target(corpus):
    for q in corpus:
        if q["expect"]["decision"] != "clarify":
            assert q["expect"]["clarify_about"] is None, q["query_id"]


def test_underspecified_class_spans_more_than_one_underlying_request(corpus):
    """The class is orthogonal to intent: one of its templates asks only for a
    comparison, and the answer is still 'ask'. An unclear date is unclear
    whatever was requested."""
    texts = [q["text"] for q in corpus if q["intent"] == "underspecified_date"]
    assert any("queue the fixes" in x for x in texts)
    assert any("tell me what differs" in x for x in texts)


# --- session sharing, and the one place it is not allowed ---------------------
#
# The test that was missing while a comment claimed the invariant. `build` used
# a per-intent seed and a comment that said two intents therefore never draw the
# same sessions. Independent draws from one pool are not disjoint draws, 18 of
# 40 sessions carried two or three intents, and nobody found out for weeks
# because nothing asserted it.

def test_premature_stage_sessions_are_not_shared(corpus):
    """The precondition gate's only test class, and it could not fire.

    `premature_stage` asks to queue a correction before triage has recorded the
    discrepancy. The staging worker must refuse, and the supervisor must re-plan
    around the refusal -- that refusal-then-recover path is the behaviour this
    class measures and the only place in the corpus that measures it.

    `discrepancies` is keyed by session with no run or request scope. So when
    another intent on the same session ran triage first, the row existed, the
    staging call was no longer premature, and the gate stayed silent. Four of
    six were in that position, and at concurrency 4 which one won was a matter
    of scheduling: the class was partly measuring the scheduler."""
    import collections
    by_sid = collections.defaultdict(set)
    for q in corpus:
        if q.get("session_id"):
            by_sid[q["session_id"]].add(q["intent"])

    shared = {sid: sorted(ins) for q in corpus
              if q["intent"] == "premature_stage" and q.get("session_id")
              for sid in [q["session_id"]]
              if len(by_sid[sid]) > 1}
    assert not shared, (
        f"a premature_stage session is also used by another intent, so its "
        f"discrepancy row may already exist when staging is called: {shared}")


def test_premature_stage_sessions_are_distinct_from_each_other(corpus):
    """Six queries, six sessions. Two of them on one session would collide with
    each other for the same reason, and concurrently."""
    sids = [q["session_id"] for q in corpus
            if q["intent"] == "premature_stage"]
    assert len(sids) == len(set(sids)) == 6


def test_the_reservation_holds_at_other_seeds():
    """At the default seed the collision simply happened; at another seed it
    would happen differently. The same reasoning that caught the duplicate-text
    bug applies here, so the invariant is checked off the default seed too."""
    import collections

    import queries as q_mod
    for seed in (1, 2, 3, 7):
        qs = q_mod.build(seed=seed)
        by_sid = collections.defaultdict(set)
        for q in qs:
            if q.get("session_id"):
                by_sid[q["session_id"]].add(q["intent"])
        ps = {q["session_id"] for q in qs
              if q["intent"] == "premature_stage" and q.get("session_id")}
        assert len(ps) == 6, seed
        bad = {s: sorted(by_sid[s]) for s in ps if len(by_sid[s]) > 1}
        assert not bad, f"seed {seed}: {bad}"
