"""
Does the planning gate catch a broken repo?

A gate that passes on a correct repo proves nothing. Each test here breaks
exactly one claim the README makes about the supervisor and asserts the gate
notices, which is why that group blocks rather than warns.

Separate from `test_gate.py` for a mechanical reason worth knowing: these use
pytest's `monkeypatch`, and `test_gate.py` carries its own __main__ runner so it
can be executed as a plain script. A fixture-dependent test in that file passes
under pytest and fails under the script runner -- 43 collected, 8 failed -- so
suites that need fixtures live in their own file, as the other new suites do.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# --- the planning group, and whether it catches a broken repo ----------------
#
# A gate that passes on a correct repo proves nothing; the question is whether
# it fails on a broken one. Each test below breaks exactly one claim the README
# makes about the supervisor and asserts the gate notices. They are the reason
# this group blocks rather than warns.

def _planning(cfg=None):
    import gate
    return gate.check_planning(cfg or gate.load_config())


def _failures(results):
    return [r.name for r in results if not r.ok]


def test_planning_group_is_registered_and_blocking():
    import gate
    assert "planning" in gate.GROUPS
    assert gate.load_config()["severity"]["planning"] == "block"


def test_planning_passes_on_the_real_repo():
    assert not _failures(_planning())


def test_it_catches_a_supervisor_that_has_been_given_a_tool(monkeypatch):
    """The central claim: the supervisor chooses the route, not the
    privileges."""
    import tools
    monkeypatch.setitem(tools.ROLE_TOOLS, "supervisor", ("fetch_broker_statement",))
    assert "the supervisor holds no MCP tool" in _failures(_planning())


def test_it_catches_the_output_channel_becoming_a_real_tool(monkeypatch):
    import plan
    import tools
    monkeypatch.setitem(tools.TOOLS, plan.OUTPUT_CHANNEL,
                        (lambda *a: None, tools.FetchIn, "x"))
    bad = _failures(_planning())
    assert any("output schema, not a tool" in n for n in bad)


def test_it_catches_a_decision_field_that_grants_a_tool():
    """Checked against the generated JSON schema, so an alias or a computed
    field cannot slip past."""
    import gate
    cfg = gate.load_config()
    cfg["planning"]["forbidden_decision_fields"] = ["role"]
    bad = _failures(_planning(cfg))
    assert "a decision cannot name a tool or a tolerance" in bad


def test_it_catches_an_undeclared_decision_field():
    """Adding a field to the supervisor's output must be a deliberate act that
    fails CI until thresholds.yaml is updated with it."""
    import gate
    cfg = gate.load_config()
    cfg["planning"]["decision_fields"] = ["action", "role"]
    assert "the decision surface is exactly as declared" in _failures(_planning(cfg))


def test_it_catches_a_role_added_to_the_grants_but_not_the_literal(monkeypatch):
    """A role in ROLE_TOOLS and not in the Literal is a role the supervisor
    cannot route to, silently."""
    import tools
    monkeypatch.setitem(tools.ROLE_TOOLS, "settlement", ("flag_discrepancy",))
    bad = _failures(_planning())
    assert "the routable roles match the tool grants" in bad


def test_it_catches_a_role_with_no_capability_brief(monkeypatch):
    import plan
    briefs = dict(plan.ROLE_BRIEF)
    briefs.pop("staging")
    monkeypatch.setattr(plan, "ROLE_BRIEF", briefs)
    assert "every routable role has a capability brief" in _failures(_planning())


def test_it_catches_an_unbounded_loop(monkeypatch):
    """Two caps are needed. The worker cap alone does not bound the loop: a
    supervisor whose every decision is rejected invokes no worker, so the
    worker count never rises."""
    import plan
    monkeypatch.setattr(plan, "MAX_SUPERVISOR_CALLS", plan.MAX_WORKER_CALLS)
    assert "the loop is bounded by two caps" in _failures(_planning())


def test_it_catches_a_redactor_that_stopped_redacting(monkeypatch):
    """Probed with the staging gate's own refusal text, which quotes trade ids
    and prices. A benign outcome would pass whatever the redactor did."""
    import plan
    monkeypatch.setattr(plan, "redact", lambda s: s)
    bad = _failures(_planning())
    assert "worker refusals reach the supervisor redacted" in bad


def test_it_catches_ground_truth_reaching_the_prompt(monkeypatch):
    """A supervisor shown its own answer key scores perfectly and measures
    nothing."""
    import queries
    monkeypatch.setattr(queries, "PROMPT_FIELDS",
                        queries.PROMPT_FIELDS + ("expect",))
    assert "no ground truth reaches the supervisor" in _failures(_planning())


def test_it_catches_a_stub_nobody_asserts_anything_about(monkeypatch):
    import supervisor
    monkeypatch.setattr(supervisor, "POLICIES",
                        supervisor.POLICIES + ("brand_new_stub",))
    assert "every stub supervisor has expectations" in _failures(_planning())


def test_it_catches_a_corpus_that_stopped_separating_the_strategies():
    """If the three ceilings collapse, the headline number stops meaning
    anything and the corpus needs rebuilding, not the gate relaxing."""
    import gate
    cfg = gate.load_config()
    cfg["planning"]["early_stop_min"] = 999
    assert "the early-stop class is populated" in _failures(_planning(cfg))
