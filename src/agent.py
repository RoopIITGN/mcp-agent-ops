"""
The agents: three workers with scoped tools, and a supervisor that dispatches.

Four agents, and only three of them are agents. The supervisor is
deterministic: it groups sessions by account, starts the worker processes,
hands each worker its slice of the previous worker's output, and collects the
results. It makes no model call and holds no tools. Saying so plainly is worth
more than claiming agency that is really a loop -- the agency is in the
workers, and that is a complete answer.

The three workers are heterogeneous, which is the whole reason for the shape.
Each one holds a different subset of the six tools, so least privilege applies
at the agent layer and not only at the tool layer:

  reconciliation  reads both books and compares them. Holds no write tool.
  triage          classifies and records. Cannot stage a correction.
  staging         queues corrections. Cannot read a client document.

The ordering of those three is the security argument. The agent that reads
untrusted text -- the broker statement's free-text `notes` field, which is
where the injection suite puts its payloads -- is the one agent in the system
with no ability to write anything at all. A payload that fully captures it has
captured a process that can do nothing but read and report.

One server process per (account, role). Fifteen accounts means forty-five
processes over a run rather than one hundred and eighty, because an account's
four sessions share its servers -- and it means an account binding can never be
mutated, only launched. Parallelism across accounts is therefore safe by
construction, not by locking.

Every trajectory is written to reports/trajectories.jsonl as it completes, and
a re-run skips sessions already there. That is partly cost discipline and
partly that the retrieval project lost a full run to a DNS failure at scale.

Usage
  PYTHONPATH=src python src/agent.py --backend stub:ideal
  PYTHONPATH=src python src/agent.py --backend stub:miss_one --limit 5
  PYTHONPATH=src python src/agent.py --backend azure --limit 5     # costs money
  PYTHONPATH=src python src/agent.py --backend stub:ideal --fresh   # ignore resume
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import Client, StdioServerParameters                  # noqa: E402

import store                                                   # noqa: E402
import tools                                                   # noqa: E402
from backends import Backend, Done, ToolCall, Turn, make_backend  # noqa: E402
from tracing import Tracing                                   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/trajectories.jsonl"
WORKER_ROLES = ("reconciliation", "triage", "staging")
MAX_STEPS = 12          # a worker needing more than this has lost the thread;
                        # without a cap, one confused agent can spend a budget


def params(account: str, role: str, db: Path, run_id: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "src/server.py"), "--account", account, "--role", role,
              "--db", str(db), "--run-id", run_id],
    )


async def run_role(client: Client, role: str, task: dict, backend: Backend,
                   schemas: list[dict], tr: Tracing | None = None) -> list[Turn]:
    """One worker's loop: ask the backend for a step, execute it, repeat.

    A refused call is appended to the history as an error turn and the loop
    continues. That is deliberate: the refusal text names the missing
    precondition, and an agent that cannot read why it was refused will retry
    the same wrong call until the step cap stops it. Whether it recovers is one
    of the things worth measuring.
    """
    tr = tr or Tracing(None)
    history: list[Turn] = [Turn(kind="task", text=json.dumps(task, default=str))]
    with tr.role(role, len(schemas)):
        for seq in range(MAX_STEPS):
            step = backend.step(role, task, history, schemas)
            if isinstance(step, Done):
                history.append(Turn(kind="result", name="__done__", text=step.note))
                return history
            history.append(Turn(kind="call", name=step.name, args=step.args))
            with tr.tool(step.name, step.args, seq) as span:
                res = await client.call_tool(step.name, {"args": step.args})
                text = res.content[0].text if res.content else ""
                if res.is_error:
                    # ERROR status on the span, because a gate firing is the
                    # most interesting event in a trace.
                    tr.refused(span, text)
                    history.append(Turn(kind="error", name=step.name, text=text))
                    continue
                tr.ok(span, **{"tool.bytes": len(text)})
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = text
                history.append(Turn(kind="result", name=step.name, result=parsed))
        history.append(Turn(kind="error", name="__cap__",
                            text=f"stopped after {MAX_STEPS} steps"))
    return history


def observations_from(history: list[Turn]) -> list[dict]:
    """Supervisor aggregation, not agent reasoning: pull the comparison result
    out of the reconciliation transcript."""
    for t in reversed(history):
        if t.kind == "result" and t.name == "calculate_position_deltas":
            return t.result.get("observations", []) if isinstance(t.result, dict) else []
    return []


def notes_from(history: list[Turn]) -> str:
    for t in history:
        if t.kind == "result" and t.name == "fetch_broker_statement" \
                and isinstance(t.result, dict):
            return t.result.get("notes", "") or ""
    return ""


def first_trade_from(history: list[Turn]) -> str:
    for t in history:
        if t.kind == "result" and t.name == "fetch_broker_statement" \
                and isinstance(t.result, dict):
            ps = t.result.get("positions") or []
            if ps:
                return ps[0]["trade_id"]
    return "T000000"


def flagged_from(history: list[Turn]) -> list[dict]:
    """What triage actually recorded, paired with the arguments it used."""
    out, pending = [], []
    for t in history:
        if t.kind == "call" and t.name == "flag_discrepancy":
            pending.append(t.args)
        elif t.kind == "result" and t.name == "flag_discrepancy" \
                and isinstance(t.result, dict) and pending:
            a = pending.pop(0)
            out.append({"discrepancy_id": t.result["discrepancy_id"],
                        "trade_id": a.get("trade_id"), "type": a.get("type"),
                        "created": t.result.get("created")})
    return out


def serialise(history: list[Turn]) -> list[dict]:
    return [{k: v for k, v in asdict(t).items()
             if v not in ("", {}, None) or k == "kind"} for t in history]


async def run_session(session_id: str, account: str, clients: dict[str, Client],
                      backend: Backend, tr: Tracing | None = None) -> dict:
    """The supervisor's unit of work: three workers, each fed by the last."""
    t0 = time.perf_counter()
    tr = tr or Tracing(None)
    schemas = {r: tools.list_tools(r) for r in WORKER_ROLES}

    with tr.session(session_id, account, backend.label):
        recon = await run_role(clients["reconciliation"], "reconciliation",
                               {"session_id": session_id}, backend,
                               schemas["reconciliation"], tr)
        obs = observations_from(recon)

        triage = await run_role(
            clients["triage"], "triage",
            {"session_id": session_id, "observations": obs,
             "notes": notes_from(recon), "any_trade_id": first_trade_from(recon)},
            backend, schemas["triage"], tr)
        flagged = flagged_from(triage)

        # The staging worker is given what each discrepancy IS, not just its id.
        # The first version passed bare integers and told the agent to stage a
        # correction; it had no way to know what differed or what the remedy
        # was, so it staged placeholders asking to be told -- "TBD",
        # "Insufficient context provided" -- a hundred of them. An agent asked
        # to decide something it has not been shown will invent rather than
        # refuse, and that is the supervisor's failure, not the agent's.
        staging = await run_role(
            clients["staging"], "staging",
            {"session_id": session_id,
             "discrepancies": [
                 {"discrepancy_id": f["discrepancy_id"], "trade_id": f["trade_id"],
                  "type": f["type"],
                  "observed": next((o for o in obs
                                    if o.get("trade_id") == f["trade_id"]), {})}
                 for f in flagged]},
            backend, schemas["staging"], tr)

    histories = {"reconciliation": recon, "triage": triage, "staging": staging}
    refusals = {r: sum(1 for t in h if t.kind == "error") for r, h in histories.items()}
    return {
        "session_id": session_id,
        "account_id": account,
        "backend": backend.label,
        "observations": obs,
        "flagged": flagged,
        "staged": [t.args.get("discrepancy_id") for t in staging
                   if t.kind == "call" and t.name == "stage_for_execution"],
        "refusals": refusals,
        "steps": {r: sum(1 for t in h if t.kind == "call") for r, h in histories.items()},
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "trajectory": {r: serialise(h) for r, h in histories.items()},
    }


async def run_account(account: str, session_ids: list[str], backend: Backend,
                      db: Path, run_id: str, sink, tr: Tracing | None = None) -> int:
    """Three server processes, bound to this account, reused across its sessions."""
    async with Client(params(account, "reconciliation", db, run_id)) as recon, \
               Client(params(account, "triage", db, run_id)) as triage, \
               Client(params(account, "staging", db, run_id)) as staging:
        clients = {"reconciliation": recon, "triage": triage, "staging": staging}
        done = 0
        for sid in session_ids:
            rec = await run_session(sid, account, clients, backend, tr)
            rec["run_id"] = run_id
            sink.write(json.dumps(rec, default=str) + "\n")
            sink.flush()                  # flushed per session: a crash at
                                          # session 40 must not cost the first 39
            done += 1
            print(f"  {sid}  obs {len(rec['observations'])}  "
                  f"flagged {len(rec['flagged'])}  staged {len(rec['staged'])}  "
                  f"refused {sum(rec['refusals'].values())}  {rec['elapsed_s']}s")
        return done


async def amain(a) -> None:
    db = Path(a.db)
    if not db.exists():
        raise SystemExit(f"{db} does not exist -- run: python src/store.py --init")

    backend = make_backend(a.backend)
    run_id = a.run_id or f"{backend.label.replace(':', '-')}-{uuid.uuid4().hex[:6]}"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    already: set[str] = set()
    if out.exists() and not a.fresh:
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("backend") == backend.label:
                    already.add(r["session_id"])
        if already:
            print(f"resuming: {len(already)} session(s) already done for "
                  f"{backend.label}")

    con = store.connect(db)
    rows = con.execute("SELECT session_id, account_id FROM sessions "
                       "ORDER BY account_id, as_of").fetchall()
    by_account: dict[str, list[str]] = {}
    for r in rows:
        if r["session_id"] in already:
            continue
        by_account.setdefault(r["account_id"], []).append(r["session_id"])

    if a.limit:
        kept, n = {}, 0
        for acct, sids in by_account.items():
            take = sids[:max(0, a.limit - n)]
            if take:
                kept[acct] = take
                n += len(take)
            if n >= a.limit:
                break
        by_account = kept

    total = sum(len(v) for v in by_account.values())
    if not total:
        print("nothing to do")
        return
    print(f"{backend.label}: {total} session(s) across {len(by_account)} account(s), "
          f"run {run_id}\n")

    # getattr, not a.trace: calibrate.py and the tests call amain with a
    # hand-built args object, and adding a flag to the parser should not break
    # every in-process caller. The gate caught exactly that when --trace landed.
    trace_path = getattr(a, "trace", "") or ""
    tr = Tracing(Path(trace_path) if trace_path else None)
    if tr.enabled:
        print(f"tracing -> {trace_path}")

    t0 = time.perf_counter()
    with out.open("a", encoding="utf-8") as sink:
        done = 0
        for acct, sids in by_account.items():
            print(f"{acct}")
            done += await run_account(acct, sids, backend, db, run_id, sink, tr)
    tr.shutdown()

    print(f"\n{done} session(s) in {time.perf_counter() - t0:.1f}s -> {out}")
    if tr.enabled:
        from tracing import summarise
        s = summarise(Path(trace_path))
        print(f"trace: {s['spans']} span(s) over {s['traces']} trace(s), "
              f"{s['tool_spans']} tool call(s), "
              f"{s['refused_tool_spans']} refused, "
              f"p50 {s['tool_ms_p50']}ms")
    if hasattr(backend, "prompt_tokens"):
        pt, ct = backend.prompt_tokens, backend.completion_tokens
        print(f"model calls {backend.calls}, {pt:,} prompt + {ct:,} completion tokens")
        print(f"at gpt-4o-mini rates that is about "
              f"${pt/1e6*0.15 + ct/1e6*0.60:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="stub:ideal",
                    help="stub:<policy> or azure")
    ap.add_argument("--db", default=str(store.DB_PATH))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--limit", type=int, default=0, help="first N sessions only")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--fresh", action="store_true",
                    help="do not skip sessions already in the output file")
    ap.add_argument("--trace", default="",
                    help="write OpenTelemetry spans to this JSONL file "
                         "(e.g. reports/trace.jsonl); off by default")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
