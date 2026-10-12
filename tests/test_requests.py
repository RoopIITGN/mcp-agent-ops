"""
Tests for the request-driven runner: the two projections, and concurrency.

These are the slowest suite in the repo because they launch real MCP server
subprocesses. That is the point: everything above this file tests the loop with
a simulated worker, and simulation cannot tell you whether two workers writing
one SQLite file at the same time actually works.

The three that matter:

  `test_concurrent_identical_requests_converge_on_one_row_set` -- safety under
  concurrency comes from the UNIQUE constraints, not from locking. Two requests
  doing the same work must leave one row set, and the same property is what
  makes a retried request safe.

  `test_concurrent_requests_for_different_clients_do_not_mix` -- the isolation
  gate, under the condition it was designed for. The account is in argv at
  launch, so this should hold by construction; a test is how you find out it
  does.

  `test_the_supervisor_projection_carries_no_book_data` -- the context boundary
  against a real transcript rather than a hand-built one.
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
import run_requests as rr  # noqa: E402
import store  # noqa: E402
from backends import Turn  # noqa: E402
from supervisor import make_supervisor, orchestrate  # noqa: E402


def run(coro):
    """asyncio.run, with anyio's TaskGroup wrapper unpacked.

    The MCP client raises inside a task group, so a failed assertion arrives as
    an ExceptionGroup whose message hides the real one. CI spent a cycle on
    exactly that before the worker suites learned to unwrap it."""
    try:
        return asyncio.run(coro)
    except BaseExceptionGroup as eg:                              # noqa: F821
        raise eg.exceptions[0] from None


@pytest.fixture(scope="module")
def corpus():
    path = ROOT / "data/queries.json"
    if not path.exists():
        pytest.skip("data/queries.json not generated")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    """A fresh database per test. Shared state is what these tests are about, so
    sharing it between them would make failures depend on ordering."""
    path = tmp_path / "ops.db"
    store.init_db(path=path).close()
    return path


def dirty(corpus: list[dict]) -> dict:
    return next(q for q in corpus
                if q["expect"]["roles"] == list(plan.ROLES)
                and q["intent"] == "full_reconcile")


# --- projection one: counts for the supervisor --------------------------------

def test_outcome_counts_observations_for_reconciliation():
    history = [
        Turn(kind="call", name="calculate_position_deltas", args={}),
        Turn(kind="result", name="calculate_position_deltas",
             result={"observations": [{"trade_id": "T1"}, {"trade_id": "T2"}]}),
        Turn(kind="result", name="__done__", text="finished"),
    ]
    o = rr.outcome_from("reconciliation", history)
    assert o.observations == 2 and o.flagged == 0 and o.staged == 0
    assert o.stopped == "done"
    assert o.tools_called == ["calculate_position_deltas"]


def test_outcome_counts_staged_actions():
    history = [
        Turn(kind="call", name="stage_for_execution", args={"discrepancy_id": 1}),
        Turn(kind="result", name="stage_for_execution",
             result={"action_id": 1, "status": "PENDING_HUMAN"}),
    ]
    assert rr.outcome_from("staging", history).staged == 1


def test_outcome_carries_refusals_but_not_harness_errors():
    """A gate firing is a fact the supervisor needs. A step cap or a backend
    failure is a harness condition, reported through `stopped` instead, so it
    does not read as a refusal the supervisor could re-plan around."""
    history = [
        Turn(kind="error", name="stage_for_execution",
             text="discrepancy 9 does not exist -- call flag_discrepancy first"),
        Turn(kind="error", name="__cap__", text="stopped after 12 steps"),
    ]
    o = rr.outcome_from("staging", history)
    assert len(o.refusals) == 1
    assert "flag_discrepancy" in o.refusals[0]
    assert o.stopped == "cap"


def test_the_supervisor_projection_carries_no_book_data(corpus, db):
    """The boundary against a real transcript. The staging gate quotes trade ids
    in its refusals, and this is where that would reach the orchestrator if the
    redaction were not inside render()."""
    q = dirty(corpus)
    sup = make_supervisor("stub:oracle")
    backend = __import__("backends").make_backend("stub:ideal")
    details: dict[str, rr.Detail] = {}
    worker = rr.mcp_worker(backend, db, "test", details)

    captured = []

    class Watching:
        label = "watching"

        def decide(self, query, history):
            captured.append(plan.render_history(history))
            return sup.decide(query, history)

    run(orchestrate(q, Watching(), worker))
    assert len(captured) > 1
    for text in captured:
        plan.assert_no_book_data(text)


# --- projection two: detail for the next worker -------------------------------

def test_detail_threads_observations_into_triage(corpus, db):
    q = dirty(corpus)
    backend = __import__("backends").make_backend("stub:ideal")
    details: dict[str, rr.Detail] = {}
    worker = rr.mcp_worker(backend, db, "test", details)

    run(worker("reconciliation", q["session_id"], q))
    d = details[q["query_id"]]
    assert d.observations, "reconciliation produced no observations to thread"
    task = d.task_for("triage", q["session_id"])
    assert task["observations"] == d.observations
    assert "notes" in task


def test_detail_is_kept_per_request(corpus, db):
    """Two concurrent requests from the same client asking the same thing must
    not share orchestration state. No cross-client leak in that case, but one
    request's findings threaded into another's staging task is the subtler
    version of the same bug."""
    q = dirty(corpus)
    other = dict(q, query_id="Q-COPY")
    backend = __import__("backends").make_backend("stub:ideal")
    details: dict[str, rr.Detail] = {}
    worker = rr.mcp_worker(backend, db, "test", details)

    run(worker("reconciliation", q["session_id"], q))
    assert set(details) == {q["query_id"]}
    run(worker("reconciliation", other["session_id"], other))
    assert set(details) == {q["query_id"], "Q-COPY"}
    assert details[q["query_id"]] is not details["Q-COPY"]


def test_supervisor_is_never_handed_the_detail_object(corpus, db):
    """Structural, not conventional: Detail is built and read by the runner, and
    nothing passes it into a Decision or a WorkerOutcome."""
    import inspect
    src = inspect.getsource(rr.mcp_worker)
    assert "detail.task_for" in src
    outcome_src = inspect.getsource(rr.outcome_from)
    assert "Detail" not in outcome_src
    assert set(plan.WorkerOutcome.model_json_schema()["properties"]) == {
        "role", "tools_called", "observations", "flagged", "staged",
        "refusals", "stopped"}


# --- concurrency --------------------------------------------------------------

def test_wal_and_busy_timeout_are_set(db):
    """Without these, two workers racing produced "database is locked" rather
    than two correct writes -- an outage dressed as a concurrency bug."""
    con = store.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        con.close()


def _snapshot(db: Path) -> dict:
    con = store.connect(db)
    try:
        return {
            "discrepancies": [tuple(r) for r in con.execute(
                "SELECT session_id, trade_id, type, status FROM discrepancies "
                "ORDER BY session_id, trade_id, type")],
            "pending": [tuple(r) for r in con.execute(
                "SELECT session_id, discrepancy_id, status FROM pending_actions "
                "ORDER BY session_id, discrepancy_id")],
        }
    finally:
        con.close()


def test_concurrent_identical_requests_converge_on_one_row_set(corpus, db):
    """Safety under concurrency comes from the constraints, not from locking.

    Flagging is idempotent on (session, trade, type) and staging on
    (session_id, discrepancy_id), so four requests doing the same work leave the
    same rows as one. The same property is what makes a retried request safe,
    which is the at-least-once delivery story.
    """
    q = dirty(corpus)
    clones = [dict(q, query_id=f"Q-C{i}") for i in range(4)]

    async def go():
        sem = asyncio.Semaphore(4)
        return await asyncio.gather(*[
            rr.run_one(c, "stub:oracle", "stub:ideal", db, "conc", sem)
            for c in clones])

    results = run(go())
    assert all(r.invoked == q["expect"]["roles"] for r in results)
    many = _snapshot(db)

    store.init_db(force=True, path=db).close()

    async def once():
        sem = asyncio.Semaphore(1)
        return await rr.run_one(q, "stub:oracle", "stub:ideal", db, "serial", sem)

    run(once())
    one = _snapshot(db)

    assert many == one, (
        "four concurrent identical requests left different rows than one did")


def test_concurrent_requests_for_different_clients_do_not_mix(corpus, db):
    """The isolation gate under the condition it exists for. The account is in
    argv at launch, so a running worker cannot be redirected -- but construction
    arguments are a claim until something checks it."""
    picks, seen = [], set()
    for q in corpus:
        if (q["expect"]["roles"] == list(plan.ROLES) and q["session_id"]
                and q["client_id"] not in seen):
            picks.append(q)
            seen.add(q["client_id"])
        if len(picks) == 5:
            break
    assert len(picks) == 5

    async def go():
        sem = asyncio.Semaphore(5)
        return await asyncio.gather(*[
            rr.run_one(q, "stub:oracle", "stub:ideal", db, "iso", sem)
            for q in picks])

    run(go())

    con = store.connect(db)
    try:
        rows = con.execute(
            "SELECT DISTINCT d.session_id FROM discrepancies d").fetchall()
    finally:
        con.close()

    touched = {r["session_id"] for r in rows}
    allowed = {q["session_id"] for q in picks}
    assert touched <= allowed, f"touched sessions outside the requests: {touched - allowed}"
    for sid in touched:
        account = sid.rsplit("-", 3)[0]
        assert account in seen, f"{sid} belongs to a client nobody asked about"


def test_a_transport_failure_costs_one_invocation_not_the_request(corpus, db,
                                                                  monkeypatch):
    """A dead subprocess must reach the supervisor as an outcome it can act on,
    not as an exception that ends the run. The supervisor is told, so it can
    decide to stop rather than being left to infer it from an unchanged
    history."""
    q = dirty(corpus)
    backend = __import__("backends").make_backend("stub:ideal")

    def boom(*a, **k):
        raise RuntimeError("stdio closed unexpectedly")

    monkeypatch.setattr(rr, "Client", boom)
    worker = rr.mcp_worker(backend, db, "test", {})
    out = run(worker("reconciliation", q["session_id"], q))
    assert out.stopped == "backend_error"
    assert any("stdio closed" in r for r in out.refusals)
    out.render()          # must still be renderable, and still redacted


# --- the runner's own wiring --------------------------------------------------

def test_a_fresh_process_is_launched_per_invocation(corpus):
    """Per invocation, not per request and not per account. The account is in
    argv, so the binding is fixed before the process exists -- there is no code
    path that redirects a running worker."""
    p = rr.params("ACC-007", "triage", Path("data/x.db"), "run-1")
    assert "--account" in p.args
    assert p.args[p.args.index("--account") + 1] == "ACC-007"
    assert p.args[p.args.index("--role") + 1] == "triage"


def test_the_runner_writes_to_its_own_database():
    """The committed session baselines were measured against data/ops.db, and a
    request run writes discrepancy and staging rows into whatever it is pointed
    at."""
    assert rr.DB.name == "ops_requests.db"
    assert "ops_requests.db" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_an_arm_is_identified_by_its_label_not_the_cli_spec():
    """The bug both --fresh and --resume shipped with, and why it was invisible.

    A record's `supervisor` field holds the label the supervisor reports
    ("azure:gpt-5.4-nano"), because a report that cannot tell two deployments
    apart is worse than no report. The flags compared that against the CLI spec,
    which for every azure arm is the shorter "azure". The test never matched, so
    --fresh dropped nothing and --resume re-ran all eighty requests -- which is
    also precisely what both do when there is legitimately nothing to skip, so
    neither flag ever looked broken. Two paid runs landed in one file because of
    it.

    Only the stub arms, where spec and label happen to coincide, worked. So the
    assertion uses a label that differs from its spec."""
    spec = "azure:gpt-5.4-nano"
    label = "azure:gpt-5.4-nano"       # what AzureSupervisor.label reports
    assert spec != "azure", "the spec under test must differ from the label"

    lines = [json.dumps({"supervisor": label, "query_id": "Q-001"}),
             json.dumps({"supervisor": label, "query_id": "Q-002"}),
             json.dumps({"supervisor": "azure:gpt-5.4-mini",
                         "query_id": "Q-001"})]
    mine, others = rr.partition(lines, label)
    assert len(mine) == 2, "this arm's records were not recognised"
    assert len(others) == 1, "another arm's records were dropped"

    # And the comparison must not accidentally succeed on the bare spec, which
    # is what a `startswith` would do: "azure" prefixes both deployments, so a
    # prefix match would silently merge the two arms being compared.
    mine, others = rr.partition(lines, "azure")
    assert mine == [], "a bare spec must match no arm rather than all of them"
    assert len(others) == 3


def test_only_one_run_of_a_model_is_continuable():
    """What --resume means, and the splice it used to allow.

    Matching on the label alone made every past run of a model continuable, so
    a resume after a crash at request 70 would take 1-69 from one run and 70-80
    from another and the scorer would report the join as one arm -- two
    different prompts or two different days, averaged, with nothing saying so.

    It also made a deliberate repeat impossible: the second run of a model would
    skip all eighty requests as already done. A repeat is the only way to know
    whether the gap between two models is larger than the gap between two runs
    of one, so this is load-bearing for every comparison in the report."""
    lines = [json.dumps({"supervisor": "m", "query_id": "Q-001",
                         "run_id": "run-a"}),
             json.dumps({"supervisor": "m", "query_id": "Q-002",
                         "run_id": "run-a"}),
             json.dumps({"supervisor": "m", "query_id": "Q-001",
                         "run_id": "run-b"})]

    mine, _ = rr.partition(lines, "m", run_id="run-a")
    assert {json.loads(l)["query_id"] for l in mine} == {"Q-001", "Q-002"}

    mine, _ = rr.partition(lines, "m", run_id="run-b")
    assert {json.loads(l)["query_id"] for l in mine} == {"Q-001"}, \
        "a resume of one run must not pick up another run's records"

    # A run id nobody has written yet continues nothing, which is what makes an
    # unnamed re-run a fresh arm rather than a no-op.
    mine, others = rr.partition(lines, "m", run_id="run-c")
    assert mine == [] and len(others) == 3


def test_each_arm_starts_from_a_rebuilt_database_by_default():
    """The contamination that confounded two measured prompt revisions.

    `discrepancies` and `pending_actions` are keyed by session with no run
    scope, which the idempotency gate needs and which also means they outlive a
    run. Revision 1 ran against a rebuilt database and revision 2 against
    revision 1's leftovers, so every precondition revision 1 earned was already
    satisfied for revision 2, and the ten points between them cannot be
    attributed. The report could never have shown it: the report does not see
    the database.

    Rebuilding restores the books from the committed JSON and clears only the
    tables an agent writes, so it resets the state under test without changing
    what is tested. Hence the default, rather than a flag to remember -- the
    first version of this was a line in a command block that I left out once."""
    import inspect
    src = inspect.getsource(rr.amain)
    assert "init_db(force=True" in src
    assert "a.keep_db" in src

    ap_src = inspect.getsource(rr.main)
    assert "--keep-db" in ap_src, "the opt-out must be explicit, not implicit"
    # And the dangerous combination has to warn rather than pass quietly.
    assert "warning:" in src


def test_an_unnamed_rerun_skips_nothing():
    """The repeat arm, and the bug that silently cancelled it.

    `partition(run_id=None)` means "every run of this arm", which is what
    --fresh needs and the opposite of what resume needs. The resume branch
    passed `a.run_id` straight through, so on a new run it matched all eighty
    records of the previous one and the process exited with "nothing to do".
    The second nano arm never ran, the variance floor was never measured, and
    the report gave no sign of it -- it simply had one arm fewer than asked for.

    A repeat is the only way to know whether the gap between two models is
    bigger than the gap between two runs of one, so this is load-bearing for
    every comparison in the report."""
    import inspect
    src = inspect.getsource(rr.amain)
    assert "if a.run_id is not None:" in src, (
        "resume must only continue a named run; None is a new run, not a "
        "wildcard")
    # And the branch for the unnamed case must announce the separate arm rather
    # than skipping into it.
    assert "a new " in src and "nothing is skipped" in src


def test_an_empty_run_id_is_not_a_run_id():
    """`--run-id` defaulted to "", which compares unequal to every stored id.
    Resume therefore skipped nothing and the repeat branch was unreachable, both
    silently, because re-running everything is also the correct behaviour when
    there is nothing to skip."""
    import inspect
    assert "a.run_id = a.run_id or None" in inspect.getsource(rr.main)
    mine, others = rr.partition(
        [json.dumps({"supervisor": "m", "query_id": "Q-001", "run_id": "r"})],
        "m", run_id=None)
    assert len(mine) == 1, "run_id=None must mean 'any run of this arm'"
