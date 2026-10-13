"""Reconstruct one request end to end: the client's words, every supervisor
decision, every worker invocation, every gate that fired, the terminal answer,
and -- if the database still holds this run -- every tool call underneath."""
import argparse, json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("query_id")
ap.add_argument("--runs", default="reports/requests.jsonl")
ap.add_argument("--queries", default="data/queries.json")
ap.add_argument("--db", default="data/ops_requests.db")
ap.add_argument("--run-id", default=None, help="which arm, if the file has several")
a = ap.parse_args()

Q = {q["query_id"]: q for q in json.loads(Path(a.queries).read_text())}
if a.query_id not in Q:
    sys.exit(f"no such query {a.query_id}; ids look like Q-001")
q = Q[a.query_id]

recs = [json.loads(l) for l in Path(a.runs).read_text().splitlines() if l.strip()]
recs = [r for r in recs if r["query_id"] == a.query_id
        and (a.run_id is None or r.get("run_id") == a.run_id)]
if not recs:
    sys.exit(f"no records for {a.query_id} in {a.runs}")
if len({r.get("run_id") for r in recs}) > 1:
    print(f"note: {len(recs)} arms hold this request "
          f"({', '.join(sorted(str(r.get('run_id')) for r in recs))}). "
          f"Showing the first; pass --run-id to choose.\n")
r = recs[0]

def rule(t): print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)

rule(f"1. WHAT THE CLIENT SENT   ({a.query_id}, arm {r.get('run_id')})")
print(f"client_id : {q['client_id']}")
print(f"text      : {q['text']}")

rule("2. WHAT THE SUPERVISOR WAS *NOT* TOLD")
hidden = {k: v for k, v in q.items() if k not in ("query_id", "client_id", "text")}
print("Held out of the prompt entirely -- the scorer reads these, the model never does:")
for k, v in hidden.items():
    print(f"  {k:12s} {json.dumps(v, default=str)[:150]}")

rule("3. THE SUPERVISOR'S DECISIONS, IN ORDER")
print(f"supervisor: {r['supervisor']}   ({r['supervisor_calls']} model call(s))\n")
for i, d in enumerate(r.get("decisions", []), 1):
    print(f"  decision {i}: action={d.get('action')}  role={d.get('role') or '-'}")
    if d.get("reason"):
        print(f"      reason: {d['reason'][:220]}")
print()
if r.get("rejections"):
    print("  GATES THAT FIRED on a decision (rejected, written back into history")
    print("  so the model re-decides rather than re-emitting the same thing):")
    for rj in r["rejections"]:
        print(f"      {rj.get('gate')}: {str(rj.get('detail'))[:200]}")
else:
    print("  no decision was rejected by a gate")

rule("4. WORKER INVOCATIONS")
print("One MCP subprocess per invocation. Account and role are in argv before")
print("the process exists, so nothing can redirect a running worker.\n")
for i, inv in enumerate(r.get("invocations", []), 1):
    print(f"  invocation {i}: role={inv.get('role')}  "
          f"refused={inv.get('refused')}  stopped={inv.get('stopped') or '-'}")
print(f"\n  route as scored: {r['invoked'] or '-'}")
print(f"  worker tool refusals: {r['worker_refusals']}")

rule("5. HOW IT ENDED")
print(f"terminal action : {r.get('terminal_action') or r.get('stopped')}")
print(f"resolved date   : {r.get('resolved_date') or '-'}")
print(f"declined        : {r.get('declined') or '-'}")
print(f"stop cause      : {r.get('stopped')}")
msg = r.get("terminal_message") or ""
print(f"\nmessage returned to the client:\n  {msg[:600] or '(none)'}")

rule("6. WAS IT RIGHT?")
e = q.get("expect", {})
print(f"expected decision : {e.get('decision')}")
print(f"expected roles    : {e.get('roles')}")
print(f"expected date     : {e.get('session_date')}")
print(f"early stop wanted : {e.get('early_stop')}  cause={e.get('stop_cause')}")
print(f"declines wanted   : {e.get('declines') or '-'}")

rule("7. TOOL CALLS UNDERNEATH (from the database)")
db = Path(a.db)
if not db.exists():
    print(f"{db} does not exist")
else:
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    sid = q.get("session_id")
    rows = con.execute(
        "SELECT run_id, agent_role, seq, tool_name, ok, error, args FROM tool_calls "
        "WHERE session_id = ? ORDER BY agent_role, seq", (sid,)).fetchall()
    if not rows:
        print(f"no tool_calls for session {sid}.")
        print("The runner rebuilds the database per arm, so only the MOST RECENT")
        print("arm's tool calls survive. Re-run that arm to repopulate, or read")
        print("reports/requests.jsonl for the orchestration layer, which persists.")
    else:
        print(f"session {sid}\n")
        for t in rows:
            mark = "ok " if t["ok"] else "REFUSED"
            print(f"  [{t['agent_role']:14s} seq {t['seq']}] {mark} {t['tool_name']}")
            print(f"      args: {t['args'][:140]}")
            if t["error"]:
                print(f"      error: {t['error'][:160]}")
