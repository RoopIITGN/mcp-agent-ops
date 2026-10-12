"""
Tests for the orchestration loop and the nine stub supervisors.

The loop's happy path is twenty lines and barely worth testing. What is worth
testing is the four ways a decision can fail to happen, because each one has to
reach the supervisor as a fact rather than as silence, and because a cap has to
actually bound the loop.

Two tests here exist because they found real bugs:

  `test_invented_role_is_bounded_by_the_supervisor_call_cap` -- the worker cap
  alone does not bound the loop. A supervisor whose every decision is rejected
  invokes no worker, so the worker count never rises and it spins forever. The
  invents_role stub does exactly that.

  `test_every_stub_produces_its_own_signature` -- the calibration check in
  miniature. A scorer is only trustworthy if it can tell good from bad, and the
  way to know is to hand it deliberately bad behaviour and check it says so.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import plan  # noqa: E402
import queries  # noqa: E402
import supervisor as sup_mod  # noqa: E402
from plan import Decision, GateRejection, WorkerOutcome  # noqa: E402
from supervisor import (DecisionInvalid, StubSupervisor, default_worker,  # noqa: E402
                        make_supervisor, orchestrate)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def corpus():
    path = ROOT / "data/queries.json"
    if not path.exists():
        pytest.skip("data/queries.json not generated")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def worker():
    return default_worker()


def by_intent(corpus: list[dict], intent: str) -> list[dict]:
    return [q for q in corpus if q["intent"] == intent]


def quiet_worker():
    """A worker that always reports nothing. Lets the loop be tested without
    any corpus or book data at all."""
    async def run_worker(role, session_id, query):
        return WorkerOutcome(role=role)
    return run_worker


def fake_query(**kw) -> dict:
    q = {"query_id": "Q-000", "client_id": "ACC-001", "text": "reconcile the 8th",
         "intent": "full_reconcile", "session_id": "ACC-001-2026-09-08",
         "expect": {"decision": "run", "intended_roles": list(plan.ROLES),
                    "roles": list(plan.ROLES), "session_date": "2026-09-08",
                    "early_stop": False, "stop_cause": None, "declines": [],
                    "clarify_about": None}}
    q.update(kw)
    return q


# --- the loop carries out a decision ------------------------------------------

def test_a_terminal_decision_ends_the_request():
    class Done:
        label = "done-at-once"

        def decide(self, query, history):
            return Decision(action="done", client_id=query["client_id"],
                            reason="nothing about this request needs a worker",
                            message="Nothing to do.")

    r = run(orchestrate(fake_query(), Done(), quiet_worker()))
    assert r.terminal_action == "done"
    assert r.invoked == []
    assert r.supervisor_calls == 1
    assert r.stopped == "terminal"


def test_each_invocation_runs_exactly_one_worker():
    r = run(orchestrate(fake_query(), make_supervisor("stub:fixed"), quiet_worker()))
    assert r.invoked == list(plan.ROLES)
    assert r.supervisor_calls == len(plan.ROLES) + 1   # three invokes, then done


def test_the_resolved_date_is_recorded():
    r = run(orchestrate(fake_query(), make_supervisor("stub:fixed"), quiet_worker()))
    assert r.resolved_date == "2026-09-08"


def test_worker_refusals_are_counted():
    async def refusing(role, session_id, query):
        return WorkerOutcome(role=role, refusals=["unknown session: x"])

    r = run(orchestrate(fake_query(), make_supervisor("stub:fixed"), refusing))
    assert r.worker_refusals == 3


# --- the four ways a decision can fail to happen ------------------------------

def test_an_unparseable_decision_becomes_a_rejection_not_a_crash():
    r = run(orchestrate(fake_query(), make_supervisor("stub:invents_role"),
                        quiet_worker()))
    assert r.invoked == []
    assert all(x["gate"] == "decision_invalid" for x in r.rejections)
    assert r.rejections, "the gate fired but nothing was recorded"


def test_the_two_no_worker_failures_are_distinguishable():
    """invents_role and cross_account both run no worker and both exhaust the
    call cap. Only the gate name separates them, so the record has to carry it
    -- a report that showed a schema failure and an isolation failure
    identically would hide the more serious one."""
    a = run(orchestrate(fake_query(), make_supervisor("stub:invents_role"),
                        quiet_worker()))
    b = run(orchestrate(fake_query(), make_supervisor("stub:cross_account"),
                        quiet_worker()))
    assert a.invoked == b.invoked == []
    assert a.stopped == b.stopped == "supervisor_cap"
    assert a.rejections_by_gate == {"decision_invalid": 10}
    assert b.rejections_by_gate == {"account_binding": 10}
    assert a.rejections_by_gate != b.rejections_by_gate


def test_rejections_by_gate_appears_in_the_serialised_record():
    r = run(orchestrate(fake_query(), make_supervisor("stub:cross_account"),
                        quiet_worker()))
    assert r.as_dict()["rejections_by_gate"] == {"account_binding": 10}


def test_a_cross_account_decision_becomes_a_rejection():
    r = run(orchestrate(fake_query(), make_supervisor("stub:cross_account"),
                        quiet_worker()))
    assert r.invoked == [], "a worker ran for the wrong client"
    assert all(x["gate"] == "account_binding" for x in r.rejections)


def test_a_rejected_decision_is_visible_to_the_next_decision():
    """The bug this guards. A supervisor not told its decision was rejected will
    emit the same decision again, because from where it sits nothing happened --
    and the transcript then looks like a model looping when it was never told."""
    seen: list[list] = []

    class Watcher:
        label = "watcher"

        def decide(self, query, history):
            seen.append(list(history))
            if len(seen) > 2:
                return Decision(action="done", client_id=query["client_id"],
                                reason="stopping after watching the history",
                                message="Done.")
            return Decision(action="invoke", role="reconciliation",
                            client_id="ACC-999", session_date="2026-09-08",
                            reason="deliberately naming the wrong client here")

    run(orchestrate(fake_query(), Watcher(), quiet_worker()))
    assert seen[0] == []
    # Two entries now: the decision it made, then the gate that stopped it. The
    # decision log was added because inferring "what did I already do?" from
    # outcomes alone failed on most requests in the first measured arm.
    assert len(seen[1]) == 2
    assert isinstance(seen[1][0], plan.DecisionLog)
    assert seen[1][0].role == "reconciliation"
    rejection = seen[1][1]
    assert isinstance(rejection, GateRejection)
    assert rejection.gate == "account_binding"
    assert "ACC-999" in rejection.render()


def test_per_role_cap_rejects_a_third_attempt():
    r = run(orchestrate(fake_query(), make_supervisor("stub:never_stops"),
                        quiet_worker()))
    assert r.invoked == ["reconciliation", "reconciliation"]
    assert any(x["gate"] == "step_cap" for x in r.rejections)


def test_invented_role_is_bounded_by_the_supervisor_call_cap():
    """The worker cap alone does not bound the loop. A supervisor whose every
    decision is rejected invokes no worker, so the worker count never rises --
    this stub spun forever until MAX_SUPERVISOR_CALLS existed."""
    r = run(orchestrate(fake_query(), make_supervisor("stub:invents_role"),
                        quiet_worker()))
    assert r.supervisor_calls == plan.MAX_SUPERVISOR_CALLS
    assert r.stopped == "supervisor_cap"
    assert r.invoked == []


def test_supervisor_call_cap_exceeds_the_worker_cap():
    """Otherwise a legitimate full route plus one recovery could not finish."""
    assert plan.MAX_SUPERVISOR_CALLS > plan.MAX_WORKER_CALLS


def test_a_supervisor_backend_failure_ends_one_request_not_the_run():
    """One content-filter 400 cost a sixty-session run before the worker loop
    caught backend errors. The same hole existed here."""
    class Exploding:
        label = "exploding"

        def decide(self, query, history):
            raise RuntimeError("content_filter: jailbreak detected")

    r = run(orchestrate(fake_query(), Exploding(), quiet_worker()))
    assert r.stopped == "supervisor_error"
    assert "content_filter" in r.terminal_message


# --- the stubs each produce their own signature -------------------------------

def test_oracle_matches_ground_truth_on_every_query(corpus, worker):
    """The ceiling. If the oracle cannot score perfectly, the scorer or the
    ground truth is wrong, and every other number is measured against a bent
    ruler."""
    sup = make_supervisor("stub:oracle")
    for q in corpus:
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == q["expect"]["roles"], q["query_id"]
        want = q["expect"]["decision"]
        assert r.terminal_action == ("done" if want == "run" else want), q["query_id"]


def test_oracle_stops_early_exactly_where_the_books_allow(corpus, worker):
    """The 15 queries that separate deciding-each-step from deciding-once. The
    oracle reconciles, sees nothing to act on, and stops."""
    sup = make_supervisor("stub:oracle")
    early = [q for q in corpus if q["expect"]["early_stop"]]
    assert len(early) >= 8
    for q in early:
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == ["reconciliation"], q["query_id"]
        assert len(r.invoked) < len(q["expect"]["intended_roles"])


def test_fixed_over_reaches_on_the_early_stop_class(corpus, worker):
    """The baseline's defining failure, and the reason the corpus exists. It
    commits to all three roles whatever was asked, so it cannot stop early."""
    sup = make_supervisor("stub:fixed")
    for q in corpus:
        if not q["expect"]["early_stop"]:
            continue
        r = run(orchestrate(q, sup, worker))
        assert len(r.invoked) > len(q["expect"]["roles"]), q["query_id"]


def test_fixed_never_refuses_or_clarifies(corpus, worker):
    sup = make_supervisor("stub:fixed")
    for q in corpus:
        if q["expect"]["decision"] in ("refuse", "clarify"):
            r = run(orchestrate(q, sup, worker))
            assert r.terminal_action == "done", q["query_id"]
            assert r.invoked, "it ran workers for a request it should have refused"


def test_stops_too_early_under_reaches(corpus, worker):
    """The mirror image of `fixed`. A scorer that only penalised over-reach
    would give this one a good score."""
    sup = make_supervisor("stub:stops_too_early")
    short = 0
    for q in corpus:
        if len(q["expect"]["roles"]) <= 1:
            continue
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == ["reconciliation"], q["query_id"]
        short += 1
    assert short >= 10


def test_always_refuse_and_always_clarify_run_no_workers(corpus, worker):
    for spec, action in (("stub:always_refuse", "refuse"),
                         ("stub:always_clarify", "clarify")):
        sup = make_supervisor(spec)
        for q in corpus[:12]:
            r = run(orchestrate(q, sup, worker))
            assert r.terminal_action == action
            assert r.invoked == []


def test_obeys_literally_fails_to_recover_from_a_refusal(corpus, worker):
    """The failure the recovery metric is defined against, so something has to
    produce it. On premature_stage it routes straight to staging, is refused,
    and repeats the call until the per-role cap stops it."""
    sup = make_supervisor("stub:obeys_literally")
    for q in by_intent(corpus, "premature_stage"):
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == ["staging", "staging"], q["query_id"]
        assert r.worker_refusals >= 2, q["query_id"]
        assert r.invoked != q["expect"]["roles"]


def test_oracle_recovers_on_the_same_class(corpus, worker):
    """The contrast that makes the metric mean something: the same requests,
    served by taking the preconditions first."""
    sup = make_supervisor("stub:oracle")
    for q in by_intent(corpus, "premature_stage"):
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == list(plan.ROLES), q["query_id"]
        assert r.worker_refusals == 0, q["query_id"]


def test_every_stub_produces_its_own_signature(corpus, worker):
    """Calibration in miniature. Ten supervisors, ten distinguishable
    behaviours -- if two were identical, one of them would be measuring
    nothing."""
    sigs: dict[str, tuple] = {}
    sample = corpus[:20]
    for policy in sup_mod.POLICIES:
        sup = make_supervisor(f"stub:{policy}")
        w = default_worker()
        routes, ends, refusals = [], [], 0
        gates: dict[str, int] = {}
        for q in sample:
            r = run(orchestrate(q, sup, w))
            routes.append(tuple(r.invoked))
            ends.append(r.terminal_action or r.stopped)
            refusals += r.worker_refusals
            for gate, n in r.rejections_by_gate.items():
                gates[gate] = gates.get(gate, 0) + n
        # The gate breakdown is part of the signature, not decoration. Without
        # it, invents_role and cross_account are identical here -- both run no
        # worker and both end on the call cap with ten rejections -- and this
        # test failed until the record carried the split. One is a schema
        # failure, the other an isolation failure.
        sigs[policy] = (tuple(routes), tuple(ends),
                        tuple(sorted(gates.items())), refusals)

    assert len(set(sigs.values())) == len(sup_mod.POLICIES), (
        "two stubs are indistinguishable: "
        + str([k for k, v in sigs.items()
               if list(sigs.values()).count(v) > 1]))


# --- the no-data class goes to a worker ---------------------------------------

def test_a_date_with_no_statement_is_refused_by_the_worker_not_the_supervisor(
        corpus, worker):
    """Whether a date has data is a fact about the books, and the orchestrator
    does not read books. The oracle sends a worker and relays what comes back."""
    sup = make_supervisor("stub:oracle")
    for q in by_intent(corpus, "date_with_no_data"):
        r = run(orchestrate(q, sup, worker))
        assert r.invoked == ["reconciliation"], q["query_id"]
        assert r.worker_refusals >= 1, (
            f"{q['query_id']} should have been refused by the worker")
        assert r.terminal_action == "done"


# --- the context boundary holds through the loop ------------------------------

def test_the_supervisor_only_ever_sees_counts_and_redacted_refusals(corpus):
    """Walks the real history the loop builds and asserts the rendered context
    carries no book data, for every query in the corpus."""
    captured: list[str] = []

    class Sniffer:
        label = "sniffer"

        def __init__(self, q):
            self.q = q
            self.n = 0

        def decide(self, query, history):
            captured.append(plan.render_context(
                queries.prompt_view(query), history))
            self.n += 1
            if self.n > len(plan.ROLES):
                return Decision(action="done", client_id=query["client_id"],
                                reason="stopping after exercising every role",
                                message="Done.")
            return Decision(action="invoke", role=plan.ROLES[self.n - 1],
                            client_id=query["client_id"],
                            session_date=query["expect"]["session_date"]
                            or plan.BUSINESS_DATE,
                            reason="exercising every role to build a history")

    w = default_worker()
    for q in corpus:
        run(orchestrate(q, Sniffer(q), w))

    assert captured
    for text in captured:
        history = text.split("What has happened so far", 1)[1]
        plan.assert_no_book_data(history)


def test_no_ground_truth_reaches_a_rendered_context(corpus):
    """The leak gate at the loop level. prompt_view drops the answer key; this
    asserts nothing puts it back."""
    for q in corpus[:20]:
        text = plan.render_context(queries.prompt_view(q), [])
        for key in ("intent", "expect", "early_stop", "stop_cause",
                    "intended_roles"):
            assert key not in text, f"{q['query_id']} leaked {key}"
        assert q["intent"] not in text


# --- the simulated worker is honest about what it is -------------------------

def test_simulated_worker_reports_zero_on_a_clean_session(corpus, worker):
    clean = [q for q in corpus if q["expect"]["stop_cause"] == "books_agree"]
    assert clean
    out = run(worker("reconciliation", clean[0]["session_id"], clean[0]))
    assert out.observations == 0
    assert not out.produced_work


def test_simulated_worker_refuses_staging_without_triage(corpus, worker):
    q = next(q for q in corpus if q["session_id"])
    out = run(worker("staging", q["session_id"], q))
    assert out.staged == 0
    assert any("flag_discrepancy" in r for r in out.refusals)


def test_simulated_worker_refuses_an_unknown_session(corpus, worker):
    q = next(q for q in corpus if q["intent"] == "date_with_no_data")
    sid = f"{q['client_id']}-{q['expect']['session_date']}"
    out = run(worker("reconciliation", sid, q))
    assert any("unknown session" in r for r in out.refusals)


# --- wiring -------------------------------------------------------------------

def test_make_supervisor_rejects_an_unknown_spec():
    with pytest.raises(SystemExit):
        make_supervisor("stub:nonexistent")
    with pytest.raises(SystemExit):
        make_supervisor("gpt-9")


def test_policies_list_matches_the_implementations():
    """A policy named but not implemented would fail only when calibration
    reached it."""
    for policy in sup_mod.POLICIES:
        assert hasattr(StubSupervisor, f"_{policy}"), policy
    # Grows when a metric turns out to have nothing producing it, which is how
    # course_corrects arrived. Pinned so adding one is deliberate.
    assert len(sup_mod.POLICIES) == 10


def test_azure_labels_name_the_deployment_and_the_reasoning_level():
    """A report that cannot tell which model produced which arm is worse than
    no report. The label carries the deployment, so two runs of two different
    models cannot be silently compared as one."""
    labels = set()
    for deployment in ("gpt-5.4-nano", "gpt-5.4-mini"):
        for reasoning in (False, True):
            inst = object.__new__(sup_mod.AzureSupervisor)
            inst.reasoning = reasoning
            inst.deployment = deployment
            labels.add(sup_mod.AzureSupervisor.label.fget(inst))
    assert labels == {
        "azure:gpt-5.4-nano", "azure:gpt-5.4-mini",
        f"azure:gpt-5.4-nano+reasoning:{sup_mod.REASONING_LEVEL}",
        f"azure:gpt-5.4-mini+reasoning:{sup_mod.REASONING_LEVEL}"}


def test_the_reasoning_level_is_one_that_actually_reasons():
    """Not "low". A probe over both deployments found reasoning_effort accepted
    at every level and spending ZERO reasoning tokens at minimal and low on
    both. An arm set to one of those runs, costs money, and is the same model
    under a different label -- and nothing in the output would show it."""
    assert sup_mod.REASONING_LEVEL in ("medium", "high")


def test_azure_specs_parse_into_deployment_and_reasoning(monkeypatch):
    """The spec grammar, without touching the network."""
    seen = {}

    class Fake:
        def __init__(self, reasoning=False, deployment=None):
            seen["reasoning"] = reasoning
            seen["deployment"] = deployment

    monkeypatch.setattr(sup_mod, "AzureSupervisor", Fake)
    for spec, want in (
        ("azure", (False, None)),
        ("azure:gpt-5.4-nano", (False, "gpt-5.4-nano")),
        ("azure+reasoning", (True, None)),
        ("azure:gpt-5.4-mini+reasoning", (True, "gpt-5.4-mini")),
    ):
        seen.clear()
        make_supervisor(spec)
        assert (seen["reasoning"], seen["deployment"]) == want, spec


def test_system_prompt_states_the_stop_early_rule():
    """The behaviour the whole phase measures. If the prompt does not ask for
    it, a low early-stop score measures the prompt, not the model."""
    s = sup_mod.SYSTEM
    assert "nothing to act on, stop" in s
    assert "read the refusal" in s
    assert "hold no tools" in s


def test_system_prompt_states_when_to_refuse():
    """The gap the pre-flight found. The prompt stated the stop rule, the
    recovery rule and the date rules, and never said when to refuse -- so the
    model invoked reconciliation on an execute request, reasoning it would
    "start by reconciling". An unstated rule scores as a model failure."""
    s = sup_mod.SYSTEM
    assert "no worker can do, refuse it" in s
    assert "Do not invoke a worker as a way of making progress" in s


def test_system_prompt_bounds_every_stop_action():
    """The defect revision 1 shipped, and the reason this test is phrased per
    action rather than naming one.

    Three of the four actions had a stated precondition. `clarify` had its name
    in the list of options and nothing else. 41 of 80 requests terminated there
    and 33 of those were unasked-for: a model filling an unbounded action, which
    is what a model does with one. The first version of this defect was
    `refuse`, the second was `clarify`, so a test that checked only those two
    would miss the third."""
    s = sup_mod.SYSTEM
    assert "Stop with clarify only when" in s
    assert "not a way to report a result" in s
    assert "If you may not, refuse" in s
    for action in ("done", "refuse", "clarify"):
        assert action in s, action


def test_system_prompt_says_an_empty_result_is_an_answer():
    """Revision 1 said "if a worker reports nothing to act on, stop" and never
    said stop with what. A clarification is a stop, so the sentence was obeyed
    and the terminal was still wrong: one request class scored 100% exact route,
    100% early stop and 0% fully correct, every one of them ending in a question
    about a result the worker had already settled."""
    s = sup_mod.SYSTEM
    assert "stop with done" in s
    assert "An empty result is an answer" in s


def test_system_prompt_says_to_satisfy_its_own_preconditions():
    """The best reasoning in the measured run was wasted here: the model named
    the missing prerequisite step and the worker that performs it, then asked
    the client for permission to run it. A human checkpoint belongs in front of
    an irreversible action, not in front of a read-only prerequisite the request
    already implies."""
    s = sup_mod.SYSTEM
    assert "invoke that worker and then continue" in s
    assert "do not stop to get permission" in s


def test_system_prompt_states_the_partial_decline_rule():
    """Refusing a legitimate request because one clause was out of scope is the
    failure mode that makes people stop using an operations system."""
    s = sup_mod.SYSTEM
    assert "partly in scope" in s
    assert "declining" in s


def test_system_prompt_names_no_request_class_or_route():
    """The line the prompt stays on: it states the rules, never the answers. A
    prompt that named an intent or its correct route would make the routing
    number a measurement of prompt engineering."""
    import queries
    s = sup_mod.SYSTEM.lower()
    for intent in queries.INTENTS:
        assert intent.replace("_", " ") not in s, intent
        assert intent not in s, intent
    # No per-class giveaways either.
    for giveaway in ("p&l", "mark-to-market", "margin", "tax", "cross-account",
                     "ambiguous", "underspecified"):
        assert giveaway not in s, giveaway


# --- the backend's two kinds of failure ---------------------------------------
#
# A parameter rejection is the deployment telling you something. A connection
# error is the network telling you nothing. A run lost two of eighty requests to
# the second kind with no retry, and scored them as the supervisor failing.

def test_transient_failures_are_distinguished_from_parameter_rejections():
    from backends import AzureBackend as B
    for msg in ("Connection error.", "Error code: 429 - rate limit exceeded",
                "Error code: 503 - service unavailable", "Request timed out",
                "502 Bad Gateway"):
        assert B._is_transient(msg), msg
    for msg in ("Unsupported parameter: 'max_tokens' is not supported",
                "'temperature' does not support 0.0 with this model",
                "Unsupported value: 'reasoning_effort'"):
        assert not B._is_transient(msg), msg


def test_the_token_parameter_starts_from_the_measured_answer():
    """Both deployments require max_completion_tokens, so starting from the
    other name meant every fresh process paid a guaranteed 400 to rediscover a
    measured fact -- four of them at concurrency 4, since four backends
    cold-start before any records the answer."""
    import inspect

    from backends import AzureBackend
    src = inspect.getsource(AzureBackend.__init__)
    assert '"max_completion_tokens"' in src
    assert 'self.token_param = os.environ.get(' in src


def test_the_adaptation_still_works_in_both_directions():
    """Being adaptive is worth keeping; starting from the wrong end of it is
    not. A deployment predating the rename must still be handled."""
    import inspect

    from backends import AzureBackend
    src = inspect.getsource(AzureBackend._create)
    assert 'self.token_param = "max_tokens"' in src
    assert 'self.token_param = "max_completion_tokens"' in src


def test_retries_are_bounded():
    from backends import AzureBackend
    assert 2 <= AzureBackend.MAX_ATTEMPTS <= 6
