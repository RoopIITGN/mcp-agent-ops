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
        return res


async def amain(a) -> None:
    db = Path(a.db)
    if not db.exists():
        raise SystemExit(
            f"{db} does not exist -- run: python src/store.py --init --db {db}")

    qs = json.loads((ROOT / "data/queries.json").read_text(encoding="utf-8"))
    if a.limit:
        qs = qs[:a.limit]

    run_id = a.run_id or f"{a.supervisor.replace(':', '-')}-{uuid.uuid4().hex[:6]}"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    already: set[str] = set()
    if out.exists() and not a.fresh:
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("supervisor") == a.supervisor:
                    already.add(r["query_id"])
        if already:
            print(f"resuming: {len(already)} request(s) already done for "
                  f"{a.supervisor}")
    qs = [q for q in qs if q["query_id"] not in already]
    if not qs:
        print("nothing to do")
        return

    sem = asyncio.Semaphore(a.concurrency)
    print(f"{a.supervisor} + worker {a.backend}: {len(qs)} request(s), "
          f"concurrency {a.concurrency}, run {run_id}\n")

    t0 = time.perf_counter()
    done = 0
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
            route = ",".join(res.invoked) or "-"
            print(f"  {res.query_id}  [{route}]  "
                  f"end={res.terminal_action or res.stopped}  "
                  f"calls={res.supervisor_calls}  "
                  f"rej={len(res.rejections)}  refused={res.worker_refusals}")

    elapsed = time.perf_counter() - t0
    print(f"\n{done} request(s) in {elapsed:.1f}s "
          f"({done / elapsed:.1f}/s at concurrency {a.concurrency}) -> {out}")


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
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
