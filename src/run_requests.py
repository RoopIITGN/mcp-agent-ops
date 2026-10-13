"""
The request-driven runner: real MCP workers, one process per invocation.

`supervisor.orchestrate` takes `run_worker` as a callable so the loop can be
tested without subprocesses. This is the callable that makes it real: it launches
an MCP server bound to one client and one role, lets the worker agent choose its
own tools inside that role, and hands back counts.

A fresh server process per worker invocation, not per request and not per
account. Three consequences, and the first is the one that matters:

  the account is in argv at launch, so a running worker cannot be redirected to
  another client's book -- there is no code path, because the binding is fixed
  before the process exists;

  two requests from the same client, arriving together, share nothing: separate
  tasks, separate subprocesses, separate database connections;

  it costs roughly 150ms a call. Worth it, and cheaper than reasoning about
  whether a shared stdio stream interleaves safely under concurrency. It does
  not: stdio MCP is one request/response channel and the connection's SQLite
  handle is thread-bound.

The two projections, which is where the context boundary becomes code

One worker transcript is read twice, for two different audiences:

  `outcome_from` -> counts and redacted refusals, for the SUPERVISOR. It decides
  whether to continue, so that is all it gets.

  `Detail` -> observations and flagged ids, for the NEXT WORKER. Triage needs
  the actual differences and staging needs the actual discrepancy ids, and the
  orchestration layer threads them.

So the supervisor decides WHETHER and the plumbing decides WHAT. Keeping those
in separate objects built by separate functions is what stops the boundary
eroding the first time something is convenient.

Usage
  python src/store.py --init --db data/ops_requests.db
  PYTHONPATH=src python src/run_requests.py --supervisor stub:oracle
  PYTHONPATH=src python src/run_requests.py --supervisor stub:oracle --concurrency 8
  PYTHONPATH=src python src/run_requests.py --supervisor azure --limit 10   # costs money
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import Client, StdioServerParameters                     # noqa: E402

import agent                                                      # noqa: E402
import store                                                      # noqa: E402
import tools                                                      # noqa: E402
from backends import Turn, make_backend                           # noqa: E402
from plan import WorkerOutcome                                    # noqa: E402
from supervisor import Result, make_supervisor, orchestrate       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/ops_requests.db"
OUT = ROOT / "reports/requests.jsonl"


# --- projection one: what the supervisor is told ------------------------------

def outcome_from(role: str, history: list[Turn]) -> WorkerOutcome:
    """Counts and refusals. Nothing else leaves the worker for the supervisor.

    `refusals` carries the gate's own words because the supervisor needs to know
    WHICH precondition failed to decide whether re-planning would help.
    `WorkerOutcome.render` redacts trade ids and figures out of them, and
    asserts it did, so the text reaching the orchestrator names the missing step
    and not the trade.
    """
    called = [t.name for t in history if t.kind == "call"]
    refusals = [t.text for t in history
                if t.kind == "error" and t.name not in ("__cap__", "__backend__")]

    # Did anything come back at all? A tool that was called and returned leaves a
    # `result` turn; one that was refused leaves an `error` turn. So a worker that
    # called tools and has no results read nothing -- the session does not exist,
    # or every call was refused -- and its counts are all structurally zero for a
    # reason that has nothing to do with the books agreeing.
    #
    # This is computed here rather than left to the supervisor because the
    # supervisor got it wrong on a paid run and told a client their books were
    # clean on a date that holds no statement. `WorkerOutcome.read_ok` is what
    # stops the count being printed as a finding.
    read_ok = any(t.kind == "result" and t.name != "__done__" for t in history)

    stopped = "done"
    for t in history:
        if t.kind == "error" and t.name == "__cap__":
            stopped = "cap"
        elif t.kind == "error" and t.name == "__backend__":
            stopped = "backend_error"

    counts = {"observations": 0, "flagged": 0, "staged": 0}
    if role == "reconciliation":
        counts["observations"] = len(agent.observations_from(history))
    elif role == "triage":
        counts["flagged"] = len(agent.flagged_from(history))
    else:
        counts["staged"] = sum(
            1 for t in history
            if t.kind == "result" and t.name == "stage_for_execution")

    return WorkerOutcome(role=role, tools_called=called, refusals=refusals,
                         read_ok=read_ok,
                         stopped=stopped, **counts)


# --- projection two: what the next worker is given ----------------------------

@dataclass
class Detail:
    """The orchestration layer's own state. Never shown to the supervisor.

    This is the half of the transcript the supervisor must not see. Triage needs
    the observations to classify and staging needs the discrepancy ids to act
    on, and threading them here is what lets the supervisor be told counts alone
    without crippling the workers.
    """

    observations: list[dict] = field(default_factory=list)
    notes: str = ""
    any_trade: str = "T000000"
    flagged: list[dict] = field(default_factory=list)

    def absorb(self, role: str, history: list[Turn]) -> None:
        if role == "reconciliation":
            self.observations = agent.observations_from(history)
            self.notes = agent.notes_from(history)
            self.any_trade = agent.first_trade_from(history)
        elif role == "triage":
            self.flagged = agent.flagged_from(history)

    def task_for(self, role: str, session_id: str) -> dict:
        if role == "reconciliation":
            return {"session_id": session_id}
        if role == "triage":
            return {"session_id": session_id, "observations": self.observations,
                    "notes": self.notes, "any_trade_id": self.any_trade}
        return {"session_id": session_id,
                "discrepancies": [
                    {"discrepancy_id": f["discrepancy_id"],
                     "trade_id": f["trade_id"], "type": f["type"],
                     "observed": next((o for o in self.observations
                                       if o.get("trade_id") == f["trade_id"]), {})}
                    for f in self.flagged]}


# --- the worker ---------------------------------------------------------------

def params(client_id: str, role: str, db: Path, run_id: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "src/server.py"), "--account", client_id, "--role", role,
              "--db", str(db), "--run-id", run_id])


def mcp_worker(backend, db: Path, run_id: str, details: dict[str, Detail]):
    """Build the callable `orchestrate` invokes.

    `details` is keyed by query id, so two concurrent requests keep separate
    orchestration state even when they are the same client asking the same
    thing. Sharing it would be the subtlest version of the bug this design
    exists to avoid: no cross-client leak, but one request's findings threaded
    into another's staging task.
    """
    async def run(role: str, session_id: str, query: dict) -> WorkerOutcome:
        qid = query["query_id"]
        detail = details.setdefault(qid, Detail())
        task = detail.task_for(role, session_id)

        try:
            async with Client(params(query["client_id"], role, db, run_id)) as c:
                history = await agent.run_role(c, role, task, backend,
                                               tools.list_tools(role))
        except Exception as e:                                    # noqa: BLE001
            # A dead subprocess or a transport failure costs this invocation,
            # not the request and not the run. The supervisor is told, so it can
            # decide whether to retry or stop rather than being left to guess
            # from an unchanged history.
            return WorkerOutcome(role=role, stopped="backend_error",
                                 refusals=[f"{type(e).__name__}: {str(e)[:200]}"])

        detail.absorb(role, history)
        return outcome_from(role, history)

    return run


# --- the run ------------------------------------------------------------------

async def run_one(query: dict, sup_spec: str, backend_spec: str, db: Path,
                  run_id: str, sem: asyncio.Semaphore) -> Result:
    """One request, in its own task with its own everything.

    The supervisor and backend are built per request rather than shared. A
    shared backend object would accumulate token counters across concurrent
    requests, which is harmless, and message state, which is not.
    """
    async with sem:
        sup = make_supervisor(sup_spec)
        backend = make_backend(backend_spec)
        details: dict[str, Detail] = {}
        worker = mcp_worker(backend, db, run_id, details)
        res = await orchestrate(query, sup, worker)
        if hasattr(backend, "prompt_tokens"):
            res.worker_tokens = (backend.prompt_tokens, backend.completion_tokens)
        # The supervisor's own spend, which nothing was recording. The first
        # measured arm finished with no idea what it had cost, which makes the
        # next arm's budget a guess.
        inner = getattr(sup, "_backend", None)
        if inner is not None and hasattr(inner, "prompt_tokens"):
            res.supervisor_tokens = (inner.prompt_tokens, inner.completion_tokens)
        return res


def read_records(out: Path) -> list[str]:
    """The non-blank lines of a results file, unparsed."""
    return [line for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def partition(lines: list[str], label: str,
              run_id: str | None = None) -> tuple[list[str], list[str]]:
    """Split records into this arm's and every other arm's.

    One function because --fresh and --resume are the same question asked for
    opposite purposes, and when they were two inlined comparisons they were
    wrong in the same way at the same time.

    `run_id` narrows "this arm" to one run of it. Without it, --resume treated
    every past run of a model as continuable, so a resume after a crash would
    splice request 1-69 of one run onto 70-80 of another and the scorer would
    report the join as a single arm. It also left no way to run the same model
    twice on purpose, which is the only way to know whether a difference between
    two models is bigger than the difference between two runs of one.
    """
    def mine_(line: str) -> bool:
        r = json.loads(line)
        return (r.get("supervisor") == label
                and (run_id is None or r.get("run_id") == run_id))

    return ([line for line in lines if mine_(line)],
            [line for line in lines if not mine_(line)])


async def amain(a) -> None:
    db = Path(a.db)

    # EVERY ARM GETS A FRESH DATABASE, and this default is the whole reason.
    #
    # `discrepancies` and `pending_actions` are keyed by session with no run
    # scope -- deliberately, because the idempotency gate is a UNIQUE constraint
    # on (session_id, trade_id, type) and the concurrency claim depends on rows
    # persisting across requests within a run. The cost is that they also persist
    # BETWEEN runs.
    #
    # So an arm run second against the same file inherits the first arm's rows,
    # and every precondition the first arm earned is already satisfied for the
    # second. Two measured prompt revisions were compared that way: the first ran
    # against a rebuilt database and the second against the first's leftovers,
    # and the ten-point difference between them cannot be attributed. Nothing in
    # the report could have shown it, because the report never sees the database.
    #
    # Rebuilding is cheap -- the books are regenerated from a seed -- so it is
    # the default rather than a flag to remember. --keep-db is for resuming a
    # crashed run, where the rows belong to the run being continued, and the
    # combination that is almost always a mistake is called out below.
    if a.keep_db:
        if not db.exists():
            raise SystemExit(
                f"--keep-db but {db} does not exist -- drop the flag to build it")
        if a.run_id is None:
            print("warning: --keep-db without --run-id. This run will see rows "
                  "left by earlier runs, and any precondition they satisfied "
                  "will not be re-earned. Only correct when resuming.")
    else:
        # The books come back from the committed JSON; only the tables an agent
        # writes are cleared. So this resets the state under test without
        # changing what is being tested.
        store.init_db(force=True, path=db).close()
        print(f"rebuilt {db} for this arm")

    qs = json.loads((ROOT / "data/queries.json").read_text(encoding="utf-8"))
    if a.limit:
        qs = qs[:a.limit]

    run_id = a.run_id or f"{a.supervisor.replace(':', '-')}-{uuid.uuid4().hex[:6]}"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # The spec is not the label, and both --fresh and --resume compared against
    # the wrong one.
    #
    # A record stores `supervisor` as the label the supervisor reports --
    # "azure:gpt-5.4-nano" -- because a report that cannot tell two deployments
    # apart is worse than no report. `a.supervisor` is what was typed on the
    # command line, which for every azure arm is the shorter "azure". So the
    # equality test never fired: --fresh dropped nothing and said nothing, and
    # --resume re-ran all eighty requests it was meant to skip. Both looked like
    # they worked, because doing the whole run is also what they do when there
    # is genuinely nothing to skip.
    #
    # This is the same confusion as the run-id collision, one layer down, so it
    # is resolved once here and the label is what the rest of the function uses.
    # Building the supervisor costs an object, not an API call.
    label = make_supervisor(a.supervisor).label

    if a.fresh and out.exists():
        # Truncate this supervisor's records rather than appending beside them.
        #
        # --fresh used to mean only "do not skip what is already done", so a
        # re-run appended to the same file under the same label and the scorer
        # -- which deduplicated on (supervisor, query_id) -- reported the older
        # run. A measured harness fix came back with the pre-fix numbers and
        # nothing said so. Records from other supervisors are kept, because a
        # file holding several arms is the normal case.
        mine, others = partition(read_records(out), label)
        # Printed even when it drops nothing, because a no-op that says nothing
        # is how the label bug stayed invisible through two paid runs.
        print(f"--fresh: dropping {len(mine)} earlier record(s) for {label}, "
              f"keeping {len(others)} from other arms")
        out.write_text("".join(line + "\n" for line in others), encoding="utf-8")

    already: set[str] = set()
    if out.exists() and not a.fresh:
        # ONLY A NAMED RUN IS CONTINUABLE, and `None` is not a name.
        #
        # This branch used to call partition with run_id=a.run_id unconditionally.
        # In partition, run_id=None means "any run of this arm" -- which is right
        # for --fresh, whose job is to drop every record the arm has ever written,
        # and exactly wrong here. On a new run it matched all eighty records of
        # the previous run, so `already` was full and the run exited with
        # "nothing to do". The repeat arm this was built to enable did not
        # happen, and the variance floor went unmeasured while the report looked
        # complete.
        #
        # The same word meaning "this one run" to one caller and "all runs" to
        # the other is the whole bug. It is spelled out rather than shared now.
        if a.run_id is not None:
            mine, _ = partition(read_records(out), label, run_id=a.run_id)
            already = {json.loads(line)["query_id"] for line in mine}
            if already:
                print(f"resuming run {a.run_id}: {len(already)} request(s) "
                      f"already done for {label}")
        else:
            prior, _ = partition(read_records(out), label)
            if prior:
                print(f"note: {len(prior)} record(s) for {label} already in this "
                      f"file from earlier run(s). This is run {run_id}, a new "
                      f"arm: nothing is skipped and nothing is merged. Pass "
                      f"--run-id to continue an earlier run, --fresh to replace "
                      f"this arm's records.")
    qs = [q for q in qs if q["query_id"] not in already]
    if not qs:
        print("nothing to do")
        return

    sem = asyncio.Semaphore(a.concurrency)
    print(f"{a.supervisor} + worker {a.backend}: {len(qs)} request(s), "
          f"concurrency {a.concurrency}, run {run_id}\n")

    t0 = time.perf_counter()
    done = 0
    sup_pt = sup_ct = sup_calls = 0
    with out.open("a", encoding="utf-8") as sink:
        tasks = [asyncio.create_task(
            run_one(q, a.supervisor, a.backend, db, run_id, sem)) for q in qs]
        for coro in asyncio.as_completed(tasks):
            res = await coro
            rec = res.as_dict()
            rec["run_id"] = run_id
            rec["worker_backend"] = a.backend
            sink.write(json.dumps(rec, default=str) + "\n")
            sink.flush()          # per request: a crash at request 70 must not
                                  # cost the first 69
            done += 1
            if res.supervisor_tokens:
                sup_pt += res.supervisor_tokens[0]
                sup_ct += res.supervisor_tokens[1]
            sup_calls += res.supervisor_calls
            route = ",".join(res.invoked) or "-"
            print(f"  {res.query_id}  [{route}]  "
                  f"end={res.terminal_action or res.stopped}  "
                  f"calls={res.supervisor_calls}  "
                  f"rej={len(res.rejections)}  refused={res.worker_refusals}")

    elapsed = time.perf_counter() - t0
    print(f"\n{done} request(s) in {elapsed:.1f}s "
          f"({done / elapsed:.1f}/s at concurrency {a.concurrency}) -> {out}")
    if sup_pt:
        print(f"supervisor: {sup_calls} model call(s), {sup_pt:,} prompt + "
              f"{sup_ct:,} completion tokens")
        print(f"  {(sup_pt + sup_ct) / max(1, done):,.0f} tokens per request, "
              f"{sup_calls / max(1, done):.1f} decisions per request")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--supervisor", default="stub:oracle",
                    help="stub:<policy>, azure, azure+reasoning")
    ap.add_argument("--backend", default="stub:ideal",
                    help="the WORKER backend: stub:<policy> or azure")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="requests in flight at once; each holds its own "
                         "subprocess per worker invocation")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--keep-db", action="store_true",
                    help="continue against the existing database instead of "
                         "rebuilding it. Only correct when resuming a crashed "
                         "run with the same --run-id; see amain.")
    a = ap.parse_args()
    # "" is not a run id. Left as the empty string it compares unequal to every
    # stored id, which silently turned --resume into a full re-run and made the
    # 'is this a repeat?' branch unreachable.
    a.run_id = a.run_id or None
    asyncio.run(amain(a))


if __name__ == "__main__":
    main()
