"""Every request in one arm: what the client asked, what came back, what the
books actually hold, and why the answer scored the way it did.

  python review_run.py                        # list the arms in the file
  python review_run.py --arm <run_id>         # one line per request
  python review_run.py --arm <run_id> -v      # full detail per request
  python review_run.py --arm <run_id> --only wrong
  python review_run.py --arm <run_id> --intent premature_stage
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--runs", default="reports/requests.jsonl")
ap.add_argument("--queries", default="data/queries.json")
ap.add_argument("--truth", default="data/seeded_truth.json")
ap.add_argument("--db", default="data/ops_requests.db")
ap.add_argument("--arm", default=None, help="run_id; omit to list the arms")
ap.add_argument("--intent", default=None)
ap.add_argument("--only", choices=["all", "right", "wrong"], default="all")
ap.add_argument("-v", "--verbose", action="store_true")
a = ap.parse_args()

Q = {q["query_id"]: q for q in json.loads(Path(a.queries).read_text())}
TRUTH = {t["session_id"]: t for t in json.loads(Path(a.truth).read_text())}
recs = [json.loads(l) for l in Path(a.runs).read_text().splitlines() if l.strip()]

arms: dict[str, list] = {}
for r in recs:
    arms.setdefault(f"{r['supervisor']} @ {r.get('run_id')}", []).append(r)

if not a.arm:
    print(f"{len(recs)} record(s) in {a.runs}, across {len(arms)} arm(s):\n")
    for k, v in sorted(arms.items()):
        print(f"  {len(v):3d} requests   {k}")
    print("\nPick one with --arm <run_id> (the part after the @).")
    sys.exit(0)

mine = [r for r in recs if r.get("run_id") == a.arm]
if not mine:
    sys.exit(f"no records with run_id {a.arm!r}. Run without --arm to list them.")

# --- the books, which are the ground truth -----------------------------------
BOOKS: dict[str, dict] = {}
db = Path(a.db)
if db.exists():
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    tol = {r["account_id"]: r["tolerance"]
           for r in con.execute("SELECT account_id, tolerance FROM accounts")}
    for row in con.execute("SELECT * FROM sessions"):
        B = json.loads(row["broker_statement"]); L = json.loads(row["internal_ledger"])
        b = {t["trade_id"]: t for t in B["positions"]}
        l = {t["trade_id"]: t for t in L["positions"]}
        diffs = []
        for tid in sorted(set(b) | set(l)):
            if tid not in l:
                diffs.append(f"{tid} on the statement, absent from our ledger"); continue
            if tid not in b:
                diffs.append(f"{tid} in our ledger, absent from the statement"); continue
            for f in ("quantity", "price", "settlement_date"):
                bv, lv = b[tid].get(f), l[tid].get(f)
                if bv == lv: continue
                if f == "price":
                    t_ = tol.get(row["account_id"], 0)
                    pct = abs(bv - lv) / ((bv + lv) / 2)
                    diffs.append(f"{tid} price broker={bv} ledger={lv} "
                                 f"({pct:.2%} vs {t_:.2%} tolerance -> "
                                 f"{'REPORTABLE' if pct > t_ else 'inside tolerance, ignore'})")
                else:
                    diffs.append(f"{tid} {f} broker={bv} ledger={lv}")
        BOOKS[row["session_id"]] = {"n": len(b), "diffs": diffs,
                                    "note": B.get("notes") or ""}

def verdict(r, q):
    """Which part of the answer was wrong. Mirrors routing_eval.score_one."""
    e = q["expect"]
    end = r.get("terminal_action") or r.get("stopped")
    want_end = {"run": "done", "refuse": "refuse", "clarify": "clarify"}.get(
        e["decision"], e["decision"])
    bad = []
    if r["invoked"] != e["roles"]:
        bad.append(f"route {r['invoked'] or '-'} should be {e['roles'] or '-'}")
    if end != want_end:
        bad.append(f"ended in {end}, should be {want_end}")
    msg = r.get("terminal_message") or ""
    for d in e.get("declines") or []:
        if d not in (r.get("declined") or []) and d not in msg:
            bad.append(f"never declined {d}")
    return bad

rows = []
for r in sorted(mine, key=lambda x: x["query_id"]):
    q = Q[r["query_id"]]
    if a.intent and q["intent"] != a.intent: continue
    bad = verdict(r, q)
    if a.only == "right" and bad: continue
    if a.only == "wrong" and not bad: continue
    rows.append((r, q, bad))

print(f"arm {a.arm}   {len(rows)} of {len(mine)} request(s) shown\n")

if not a.verbose:
    print(f"{'id':7s} {'intent':24s} {'ended':9s} {'ok':3s} reply")
    print("-" * 110)
    for r, q, bad in rows:
        end = r.get("terminal_action") or r.get("stopped")
        msg = (r.get("terminal_message") or "").replace("\n", " ")
        print(f"{r['query_id']:7s} {q['intent']:24s} {end:9s} "
              f"{'yes' if not bad else 'NO ':3s} {msg[:66]}")
    print(f"\n{sum(1 for *_, b in rows if not b)} right, "
          f"{sum(1 for *_, b in rows if b)} wrong. Add -v for the full picture.")
    sys.exit(0)

for r, q, bad in rows:
    sid = q.get("session_id")
    bk = BOOKS.get(sid)
    t = TRUTH.get(sid, {})
    print("=" * 100)
    print(f"{r['query_id']}   intent={q['intent']}   {'CORRECT' if not bad else 'WRONG'}")
    print("=" * 100)
    print(f"\nCLIENT ASKED ({q['client_id']}):\n  {q['text']}")
    print(f"\nREPLY THE CLIENT GOT:\n  {(r.get('terminal_message') or '(none)')[:500]}")
    print(f"\nWHAT THE BOOKS ACTUALLY HOLD  (session {sid})")
    if bk is None:
        print("  not in the database -- the runner rebuilds it per arm, so only the")
        print("  most recent arm's session rows are guaranteed present")
    else:
        print(f"  {bk['n']} trade(s) on the statement")
        if bk["diffs"]:
            for d in bk["diffs"]: print(f"  DIFFERS: {d}")
        else:
            print("  every trade agrees -- nothing to act on")
        if bk["note"]: print(f"  counterparty note: {bk['note'][:160]}")
    if t.get("discrepancies"):
        print(f"  seeded truth: {len(t['discrepancies'])} discrepancy(ies) -- "
              + ", ".join(f"{d['trade_id']} {d['type']}" for d in t["discrepancies"]))
    print(f"\nWHAT SHOULD HAVE HAPPENED")
    e = q["expect"]
    print(f"  client's intent    : {e['decision']}")
    print(f"  roles the request implies : {e['intended_roles']}")
    print(f"  roles the BOOKS justify   : {e['roles']}"
          + ("   <- fewer, because the books gave nothing to act on"
             if e.get("early_stop") else ""))
    print(f"  date               : {e['session_date']}")
    if e.get("clarify_about"): print(f"  should ask about   : {e['clarify_about']}")
    if e.get("declines"):      print(f"  should decline     : {e['declines']}")
    print(f"\nWHAT ACTUALLY HAPPENED")
    print(f"  supervisor decisions ({r['supervisor_calls']} model call(s)):")
    for i, d in enumerate(r.get("decisions", []), 1):
        print(f"    {i}. {d.get('action')} {d.get('role') or ''}"
              + (f"  -- {d.get('reason','')[:150]}" if d.get("reason") else ""))
    for rj in r.get("rejections", []):
        print(f"    GATE {rj.get('gate')}: {str(rj.get('detail'))[:160]}")
    print(f"  route        : {r['invoked'] or '-'}")
    print(f"  resolved date: {r.get('resolved_date') or '-'}")
    print(f"  ended        : {r.get('terminal_action') or r.get('stopped')}")
    print(f"  tool refusals inside workers: {r['worker_refusals']}")
    print(f"\nVERDICT: " + ("correct on every axis the scorer checks"
                            if not bad else "; ".join(bad)))
    print()
