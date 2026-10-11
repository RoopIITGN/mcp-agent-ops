"""
Does the gate actually block?

A gate that has only ever passed is in the same position as a scorer that has
only seen a correct trajectory: it has demonstrated nothing. So every check in
src/gate.py is exercised twice here -- once against the real repo, where it
must pass, and once against a deliberately broken copy, where it must fail and
name the breakage.

The breakages are the ones that would matter. A tool called
`approve_correction` appearing in the registry. The reconciliation agent, which
reads untrusted statement text, acquiring a write tool. A schema that lets a
caller name an account. `tools.py` calling `store.decide_action`. A truth
record pointing at a trade that no longer differs. Each of those turns a
sentence in the README into a false one, and each is a plausible consequence of
an ordinary refactor.

Nothing here touches the real corpus or the real reports: the integrity and
trace checks are pointed at temporary copies, and the structural checks mutate
the tools module in memory and put it back.

Usage
  python tests/test_gate.py
  pytest tests/test_gate.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import gate                                                    # noqa: E402
import tools                                                   # noqa: E402
import yaml                                                    # noqa: E402

CFG = yaml.safe_load((ROOT / "config/thresholds.yaml").read_text())


def failures(results) -> list[str]:
    return [r.name for r in results if not r.ok]


def names(results) -> list[str]:
    return [r.name for r in results]


# --- the gate passes on the real repo -----------------------------------------

def test_structural_passes_as_shipped():
    assert failures(gate.check_structural(CFG)) == []


def test_integrity_passes_as_shipped():
    assert failures(gate.check_integrity(CFG)) == []


def test_every_severity_group_is_declared():
    """A group with no declared severity would have its failures silently
    downgraded to warnings."""
    for g in gate.GROUPS:
        assert g in CFG["severity"], g
        assert CFG["severity"][g] in ("block", "warn")


def test_structural_checks_cover_every_declared_tool_and_parameter():
    """The config lists what must be true; this asserts the gate actually
    looks at each item rather than silently skipping one."""
    rs = names(gate.check_structural(CFG))
    for param in CFG["structural"]["forbidden_parameters"]:
        assert any(param in n for n in rs), f"nothing checks {param}"
    for role in CFG["structural"]["read_only_roles"]:
        assert any(role in n for n in rs), f"nothing checks {role}"


# --- structural: break it in memory -------------------------------------------

class patched_tools:
    """Mutate the tools registry, then put it back."""

    def __init__(self, TOOLS=None, ROLE_TOOLS=None):
        self.new = {"TOOLS": TOOLS, "ROLE_TOOLS": ROLE_TOOLS}
        self.old = {}

    def __enter__(self):
        for k, v in self.new.items():
            if v is not None:
                self.old[k] = getattr(tools, k)
                setattr(tools, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(tools, k, v)


def test_an_approval_tool_is_blocked():
    """The one addition that would void the central claim of the repo."""
    fake = dict(tools.TOOLS)
    fake["approve_correction"] = tools.TOOLS["stage_for_execution"]
    roles = dict(tools.ROLE_TOOLS)
    roles["staging"] = ("stage_for_execution", "approve_correction")
    with patched_tools(TOOLS=fake, ROLE_TOOLS=roles):
        f = failures(gate.check_structural(CFG))
    assert "no tool approves or executes anything" in f, f
    assert "the tool set is exactly the declared six" in f, f


def test_an_execute_tool_is_blocked_under_any_name_fragment():
    for name in ("execute_adjustment", "confirm_correction", "settle_trade",
                 "release_payment", "decide_item"):
        fake = dict(tools.TOOLS)
        fake[name] = tools.TOOLS["stage_for_execution"]
        roles = {**tools.ROLE_TOOLS, "staging": ("stage_for_execution", name)}
        with patched_tools(TOOLS=fake, ROLE_TOOLS=roles):
            f = failures(gate.check_structural(CFG))
        assert "no tool approves or executes anything" in f, name


def test_giving_the_reader_a_write_tool_is_blocked():
    """The agent that reads the untrusted notes field acquiring the ability to
    write is the single most consequential refactor mistake available here."""
    roles = {**tools.ROLE_TOOLS,
             "reconciliation": tools.ROLE_TOOLS["reconciliation"] + ("flag_discrepancy",)}
    with patched_tools(ROLE_TOOLS=roles):
        f = failures(gate.check_structural(CFG))
    assert "reconciliation agent holds no write tool" in f, f
    assert "each write tool is held by exactly one role" in f, f


def test_an_unreachable_tool_is_blocked():
    """A tool in the registry that no role holds is dead capability -- and the
    kind of thing that gets wired up later by accident."""
    roles = {**tools.ROLE_TOOLS, "staging": ()}
    with patched_tools(ROLE_TOOLS=roles):
        f = failures(gate.check_structural(CFG))
    assert "every tool belongs to a role" in f, f


def test_a_tool_accepting_an_account_id_is_blocked():
    from pydantic import ConfigDict, Field, create_model
    Leaky = create_model("Leaky", __config__=ConfigDict(extra="forbid"),
                         session_id=(str, ...), account_id=(str, Field(...)))
    fake = dict(tools.TOOLS)
    fn, _, desc = fake["fetch_broker_statement"]
    fake["fetch_broker_statement"] = (fn, Leaky, desc)
    with patched_tools(TOOLS=fake):
        f = failures(gate.check_structural(CFG))
    assert "no tool accepts account_id" in f, f


def test_a_tool_accepting_a_tolerance_is_blocked():
    """An agent able to widen the tolerance could make a price variance stop
    being one, without ever stating a falsehood."""
    from pydantic import ConfigDict, create_model
    Loose = create_model("Loose", __config__=ConfigDict(extra="forbid"),
                         session_id=(str, ...), tolerance=(float, 0.5))
    fake = dict(tools.TOOLS)
    fn, _, desc = fake["calculate_position_deltas"]
    fake["calculate_position_deltas"] = (fn, Loose, desc)
    with patched_tools(TOOLS=fake):
        f = failures(gate.check_structural(CFG))
    assert "no tool accepts tolerance" in f, f


def test_a_schema_allowing_extra_fields_is_blocked():
    """extra='forbid' is what makes an injected pre-approval field a validation
    error rather than a silently dropped one."""
    from pydantic import create_model
    Open = create_model("Open", discrepancy_id=(int, ...), payload=(dict, ...))
    fake = dict(tools.TOOLS)
    fn, _, desc = fake["stage_for_execution"]
    fake["stage_for_execution"] = (fn, Open, desc)
    with patched_tools(TOOLS=fake):
        f = failures(gate.check_structural(CFG))
    assert "every schema forbids extra fields" in f, f


def test_referencing_decide_action_from_tools_is_blocked():
    """Parsed, not grepped: the docstring that explains why approval is absent
    mentions the name, and a substring search would flag the comment that
    documents the check."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "src").mkdir()
    (tmp / "src/tools.py").write_text(
        "import store\n"
        "def approve(con, i):\n"
        "    return store.decide_action(con, i, True, 'agent')\n", encoding="utf-8")
    old = gate.ROOT
    gate.ROOT = tmp
    try:
        f = failures(gate.check_structural(CFG))
    finally:
        gate.ROOT = old
    assert "tools.py does not reference decide_action" in f, f


def test_a_docstring_mentioning_decide_action_is_not_blocked():
    """The inverse: prose about the gate must stay free."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "src").mkdir()
    (tmp / "src/tools.py").write_text(
        '"""store.decide_action exists and is wrapped by nothing here."""\n'
        "# decide_action is deliberately not exposed\n"
        "X = 1\n", encoding="utf-8")
    old = gate.ROOT
    gate.ROOT = tmp
    try:
        f = failures(gate.check_structural(CFG))
    finally:
        gate.ROOT = old
    assert "tools.py does not reference decide_action" not in f, f


# --- integrity: break the corpus ----------------------------------------------

class patched_data:
    """Point the gate at a copy of data/, so the real corpus is never touched."""

    def __init__(self, mutate):
        self.mutate = mutate

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp()) / "data"
        shutil.copytree(ROOT / "data", self.tmp,
                        ignore=shutil.ignore_patterns("*.db*"))
        self.mutate(self.tmp)
        self.old = gate.DATA
        gate.DATA = self.tmp
        return self

    def __exit__(self, *exc):
        gate.DATA = self.old


def test_truth_missing_a_session_is_blocked():
    def drop_one(d: Path):
        t = json.loads((d / "seeded_truth.json").read_text())
        (d / "seeded_truth.json").write_text(json.dumps(t[:-1]))

    with patched_data(drop_one):
        f = failures(gate.check_integrity(CFG))
    assert "truth covers exactly the corpus" in f, f


def test_a_truth_record_for_an_unknown_session_is_blocked():
    def add_ghost(d: Path):
        t = json.loads((d / "seeded_truth.json").read_text())
        t.append({"session_id": "ACC-999-2099-01-01", "account_id": "ACC-999",
                  "as_of": "2099-01-01", "discrepancies": []})
        (d / "seeded_truth.json").write_text(json.dumps(t))

    with patched_data(add_ghost):
        f = failures(gate.check_integrity(CFG))
    assert "truth covers exactly the corpus" in f, f


def test_a_session_naming_an_unknown_account_is_blocked():
    def rename(d: Path):
        a = json.loads((d / "accounts.json").read_text())
        a[0]["account_id"] = "ACC-XXX"
        (d / "accounts.json").write_text(json.dumps(a))

    with patched_data(rename):
        f = failures(gate.check_integrity(CFG))
    assert "every session names a known account" in f, f


def test_a_dropped_ceiling_is_blocked():
    """The failure mode that matters most and shows least: the ledger is
    repaired so a seeded discrepancy no longer differs. Nothing errors, every
    test passes, and recall silently acquires a ceiling below 1.0."""
    def heal(d: Path):
        sessions = json.loads((d / "sessions.json").read_text())
        truth = json.loads((d / "seeded_truth.json").read_text())
        target = next(t for t in truth if t["discrepancies"])
        s = next(x for x in sessions if x["session_id"] == target["session_id"])
        tid = target["discrepancies"][0]["trade_id"]
        broker = {p["trade_id"]: p for p in s["broker_statement"]["positions"]}
        s["internal_ledger"]["positions"] = [
            dict(broker[p["trade_id"]]) if p["trade_id"] == tid else p
            for p in s["internal_ledger"]["positions"]
            if p["trade_id"] in broker]
        if tid not in {p["trade_id"] for p in s["internal_ledger"]["positions"]}:
            s["internal_ledger"]["positions"].append(dict(broker[tid]))
        (d / "sessions.json").write_text(json.dumps(sessions))

    with patched_data(heal):
        rs = gate.check_integrity(CFG)
    f = failures(rs)
    assert any("harness ceiling" in n for n in f), f


def test_a_noisy_control_is_blocked():
    """A control session where something now differs is no longer a control,
    and the false-positive rate built on it is meaningless."""
    def spoil(d: Path):
        sessions = json.loads((d / "sessions.json").read_text())
        truth = {t["session_id"]: t
                 for t in json.loads((d / "seeded_truth.json").read_text())}
        s = next(x for x in sessions if not truth[x["session_id"]]["discrepancies"])
        s["internal_ledger"]["positions"][0]["quantity"] += 50
        (d / "sessions.json").write_text(json.dumps(sessions))

    with patched_data(spoil):
        f = failures(gate.check_integrity(CFG))
    assert any("control sessions silent" in n for n in f), f


# --- trace --------------------------------------------------------------------

def test_a_missing_trace_is_reported():
    cfg = {**CFG, "trace": {**CFG["trace"], "path": "reports/nope.jsonl"}}
    f = failures(gate.check_trace(cfg))
    assert "a trace artefact is committed" in f, f


def test_an_orphan_span_is_reported():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "reports").mkdir()
    (tmp / "reports/trace.jsonl").write_text(
        json.dumps({"name": "tool:x", "trace_id": "a" * 32, "span_id": "b" * 16,
                    "parent_span_id": "c" * 16, "start_ns": 0, "duration_ms": 1.0,
                    "status": "UNSET", "attributes": {}}) + "\n", encoding="utf-8")
    old = gate.ROOT
    gate.ROOT = tmp
    try:
        f = failures(gate.check_trace(CFG))
    finally:
        gate.ROOT = old
    assert any("orphan" in n for n in f), f


# --- baselines ----------------------------------------------------------------

class patched_reports:
    """A reports/ directory holding one synthetic, perfect trajectory.

    These tests used to read the repo's committed reports. That made them fail
    for the wrong reason whenever the artefacts were stale or absent -- which
    is how CI went red once -- and it meant they were testing the repo's
    current state rather than the gate's logic. The trajectory is built here
    from a real session id so the truth lookup resolves, and from nothing else.
    """

    def __init__(self, backend: str = "stub:ideal", flagged: bool = True,
                 write: bool = True):
        self.backend, self.flagged, self.write = backend, flagged, write

    def __enter__(self):
        tmp = Path(tempfile.mkdtemp()) / "reports"
        tmp.mkdir(parents=True)
        if self.write:
            truth = json.loads((ROOT / "data/seeded_truth.json").read_text())
            t = next(x for x in truth if len(x["discrepancies"]) == 1)
            d = t["discrepancies"][0]
            flags = ([{"discrepancy_id": 1, "trade_id": d["trade_id"],
                       "type": d["type"], "created": True}] if self.flagged else [])
            traj = {
                "reconciliation": [
                    {"kind": "call", "name": n} for n in
                    ("fetch_broker_statement", "fetch_internal_ledger",
                     "calculate_position_deltas")],
                "triage": ([{"kind": "call", "name": "flag_discrepancy"},
                            {"kind": "result", "name": "flag_discrepancy"},
                            {"kind": "call", "name": "draft_client_inquiry"}]
                           if self.flagged else []),
                "staging": ([{"kind": "call", "name": "stage_for_execution"}]
                            if self.flagged else []),
            }
            rec = {"session_id": t["session_id"], "account_id": t["account_id"],
                   "backend": self.backend, "observations": [], "flagged": flags,
                   "staged": [1] if self.flagged else [], "refusals": {},
                   "steps": {}, "trajectory": traj}
            (tmp / "trajectories.jsonl").write_text(
                json.dumps(rec) + "\n", encoding="utf-8")
        self.old = gate.REPORTS
        gate.REPORTS = tmp
        return self

    def __exit__(self, *exc):
        gate.REPORTS = self.old


def test_a_committed_perfect_run_passes_its_floors():
    with patched_reports():
        assert failures(gate.check_baselines(CFG)) == []


def test_missing_trajectories_are_reported():
    with patched_reports(write=False):
        f = failures(gate.check_baselines(CFG))
    assert "reports/trajectories.jsonl present" in f, f


def test_a_baseline_below_its_floor_is_blocked():
    """Raise the floor above anything achievable and the check must fail,
    confirming the comparison is live rather than decorative."""
    cfg = {**CFG, "baselines": {**CFG["baselines"], "recall_min": 1.5}}
    with patched_reports():
        f = failures(gate.check_baselines(cfg))
    assert any("recall" in n for n in f), f


def test_a_run_that_found_nothing_is_blocked():
    """The failure that actually happened: tools.py reverted, every compute
    call was refused, and no session found anything. Recall goes to zero and
    the gate has to stop it."""
    with patched_reports(flagged=False):
        f = failures(gate.check_baselines(CFG))
    assert any("recall" in n for n in f), f


def test_an_unknown_baseline_backend_is_reported():
    cfg = {**CFG, "baselines": {**CFG["baselines"], "backend": "azure:nonexistent"}}
    with patched_reports():
        f = failures(gate.check_baselines(cfg))
    assert any("run present" in n for n in f), f


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
        except Exception as e:                                 # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)} tests, {failed} failed")
    return 1 if failed else 0




# --- the suite has to actually run ------------------------------------------
#
# These are meta-tests, and they exist because of a real failure rather than for
# tidiness. CI ran each suite as a script, `python tests/test_x.py`. A file with
# no __main__ runner exits 0 having executed nothing, so when two suites arrived
# using fixtures and parametrize and no runner, CI reported green over a hundred
# and twenty-three tests that never ran. Green over nothing looks exactly like
# green over everything.

def test_every_suite_contains_tests():
    """A suite that collects nothing would pass silently under any runner."""
    import re
    suites = sorted((ROOT / "tests").glob("test_*.py"))
    assert len(suites) >= 9, f"only {len(suites)} suites found"
    for f in suites:
        n = len(re.findall(r"^def test_", f.read_text(encoding="utf-8"), re.M))
        assert n > 0, f"{f.name} defines no tests"


def test_ci_runs_the_suites_with_a_real_collector():
    """The invariant that was broken. Executing suites as scripts is fine as a
    convenience, but something in CI has to collect every suite regardless of
    whether it carries its own runner."""
    wf = (ROOT / ".github/workflows/eval.yml").read_text(encoding="utf-8")
    assert "pytest tests/" in wf, (
        "CI does not run a collector over tests/ -- a suite without a __main__ "
        "runner would exit 0 having run nothing")


def test_ci_installs_the_test_dependency():
    wf = (ROOT / ".github/workflows/eval.yml").read_text(encoding="utf-8")
    assert "requirements-dev.txt" in wf
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest" in dev
    assert "-r requirements.txt" in dev, (
        "dev requirements must include the runtime list, or CI installs the "
        "test runner and none of the code's own dependencies")


def test_runtime_requirements_do_not_carry_the_test_runner():
    """The property worth keeping: the harness, the evaluation and the gate run
    on the runtime list alone, with no model and no spend."""
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest" not in runtime


def test_script_runner_sits_at_the_end_of_every_suite():
    """A __main__ block in the middle of a file runs before anything defined
    below it exists, so the script runner silently under-reports. This file hit
    exactly that: four tests appended after the block, 25 reported, 29 real.
    The script count and the collected count have to agree."""
    import re
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        src = f.read_text(encoding="utf-8")
        if '__main__' not in src:
            continue          # collected by pytest only; that is checked above
        after = src.split('if __name__ ==', 1)[1]
        assert not re.search(r"^def test_", after, re.M), (
            f"{f.name} defines tests after its __main__ block, so running it as "
            f"a script skips them")


if __name__ == "__main__":
    sys.exit(main())
