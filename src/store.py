"""
Persistence for the reconciliation system -- and the place three of the four
gates are actually enforced.

The argument this repo makes is that an agent is made safe by the shape of what
it can reach, not by what its prompt tells it to do. A prompt rule is a
suggestion that survives until the model is confused or the document it is
reading tells it otherwise. A database constraint is not negotiable.

So the gates live here, in DDL, where they can be inspected:

  Capability gate. `pending_actions.status` starts at PENDING_HUMAN and the
  only function in this module that moves it off PENDING_HUMAN is
  `decide_action`. Nothing in tools.py wraps `decide_action`, so no tool
  exposes it, so it does not appear in the agent's `list_tools` response. The
  agent cannot execute because executing is not in its vocabulary -- not
  because it was asked not to.

  Precondition gate. `pending_actions.discrepancy_id` is a NOT NULL foreign
  key into `discrepancies`. Staging a correction for a discrepancy that was
  never flagged fails at the database, which is what turns a tool-ordering
  mistake into a rejected call rather than a silently wrong row. (The status
  check -- that the discrepancy is still `flagged` -- is in tools.py, because
  SQLite cannot express it as a foreign key.)

  Idempotency gate. `pending_actions.idempotency_key` is UNIQUE, and
  `discrepancies` is UNIQUE on (session_id, trade_id, type). A retrying agent
  is normal and expected. A system that double-books because the agent retried
  is broken. Both write paths here are upserts against those constraints, so
  the second identical call reaches the same state as the first.

The fourth gate -- isolation -- cannot live here, because SQL has no notion of
"the current session". It is enforced in server.py, which binds one account per
MCP connection. What this module does provide is `session_account`, so the tool
layer can assert the binding instead of trusting an argument.

One deliberate omission: no table stores the seeded truth. Truth lives in
data/seeded_truth.json and is read only by the eval harness. If the agent's
database could see the answers, every measurement taken against it would be
worthless, and the separation is easier to defend if it is physical.

Usage
  python src/store.py --init          # build schema, load sessions + accounts
  python src/store.py --init --force  # drop and rebuild
  python src/store.py --stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "ops.db"

SCHEMA = """
CREATE TABLE accounts (
    account_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tolerance   REAL NOT NULL
);

-- One row per (account, as_of). The documents the agent reads live here as
-- JSON exactly as generated, including the empty `notes` field the injection
-- suite fills later. Storing them verbatim means an injection run changes the
-- data and nothing else.
CREATE TABLE sessions (
    session_id        TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES accounts(account_id),
    as_of             TEXT NOT NULL,
    broker_statement  TEXT NOT NULL,
    internal_ledger   TEXT NOT NULL
);

-- UNIQUE(session_id, trade_id, type) is the idempotency gate for
-- flag_discrepancy: flagging the same finding twice updates one row instead of
-- creating two, so a retry is indistinguishable from a single call.
CREATE TABLE discrepancies (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    trade_id    TEXT NOT NULL,
    type        TEXT NOT NULL,
    instrument  TEXT,
    detail      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'flagged'
                CHECK (status IN ('flagged', 'dismissed', 'resolved')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (session_id, trade_id, type)
);

-- `edited` is set by a human editing the draft outside the agent's reach.
-- draft_client_inquiry upserts only while edited = 0, so an agent re-running a
-- session cannot overwrite a human's wording. That is a smaller gate than the
-- others and it is the one most likely to matter in production.
CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    discrepancy_id  INTEGER NOT NULL REFERENCES discrepancies(id),
    body            TEXT NOT NULL,
    edited          INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (discrepancy_id)
);

-- The human-in-the-loop queue. Rows arrive PENDING_HUMAN and no tool can move
-- them. `decided_by` is NULL until a person outside the agent decides.
CREATE TABLE pending_actions (
    id               INTEGER PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    discrepancy_id   INTEGER NOT NULL REFERENCES discrepancies(id),
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING_HUMAN'
                     CHECK (status IN ('PENDING_HUMAN', 'APPROVED', 'REJECTED')),
    created_at       TEXT NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT
);

-- Every tool call, accepted or rejected. Trajectory scoring reads this, and
-- the OpenTelemetry spans in Phase 6 mirror it. Rejected calls are the
-- interesting rows: a gate that fires leaves evidence here.
CREATE TABLE tool_calls (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    agent_role  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    tool_name   TEXT NOT NULL,
    args        TEXT NOT NULL,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error       TEXT,
    ts          TEXT NOT NULL
);

CREATE INDEX idx_disc_session   ON discrepancies (session_id);
CREATE INDEX idx_pending_sess   ON pending_actions (session_id);
CREATE INDEX idx_calls_run      ON tool_calls (run_id, session_id, seq);
"""


def now() -> str:
    """Milliseconds, not seconds. Two tool calls inside one second are normal,
    and a timestamp that cannot separate them makes the audit log useless for
    ordering."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Foreign keys are OFF by default in SQLite. Without this line the
    precondition gate silently does nothing, which is the kind of failure that
    looks like success."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


# --- setup -------------------------------------------------------------------

def init_db(force: bool = False, path: Path = DB_PATH) -> sqlite3.Connection:
    if path.exists():
        if not force:
            raise SystemExit(f"{path} exists -- pass --force to rebuild")
        path.unlink()
    con = connect(path)
    con.executescript(SCHEMA)

    accounts = json.loads((DATA / "accounts.json").read_text())
    sessions = json.loads((DATA / "sessions.json").read_text())

    con.executemany(
        "INSERT INTO accounts (account_id, name, tolerance) VALUES (?, ?, ?)",
        [(a["account_id"], a["name"], a["tolerance"]) for a in accounts])
    con.executemany(
        """INSERT INTO sessions
               (session_id, account_id, as_of, broker_statement, internal_ledger)
           VALUES (?, ?, ?, ?, ?)""",
        [(s["session_id"], s["account_id"], s["as_of"],
          json.dumps(s["broker_statement"]), json.dumps(s["internal_ledger"]))
         for s in sessions])
    con.commit()
    return con


# --- reads the tool layer needs ----------------------------------------------

def session_account(con: sqlite3.Connection, session_id: str) -> str | None:
    """The account this session belongs to, per the database.

    server.py compares this against the account bound to the MCP connection.
    No tool takes an account_id, so this is the only thing that decides whose
    data a call can touch."""
    row = con.execute("SELECT account_id FROM sessions WHERE session_id = ?",
                      (session_id,)).fetchone()
    return row["account_id"] if row else None


def broker_statement(con: sqlite3.Connection, session_id: str) -> dict | None:
    row = con.execute("SELECT broker_statement FROM sessions WHERE session_id = ?",
                      (session_id,)).fetchone()
    return json.loads(row["broker_statement"]) if row else None


def internal_ledger(con: sqlite3.Connection, session_id: str) -> dict | None:
    row = con.execute("SELECT internal_ledger FROM sessions WHERE session_id = ?",
                      (session_id,)).fetchone()
    return json.loads(row["internal_ledger"]) if row else None


def tolerance(con: sqlite3.Connection, account_id: str) -> float:
    row = con.execute("SELECT tolerance FROM accounts WHERE account_id = ?",
                      (account_id,)).fetchone()
    if row is None:
        raise KeyError(account_id)
    return row["tolerance"]


def discrepancy(con: sqlite3.Connection, discrepancy_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM discrepancies WHERE id = ?",
                       (discrepancy_id,)).fetchone()


def discrepancies_for(con: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM discrepancies WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()


def pending_for(con: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM pending_actions WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()


# --- writes ------------------------------------------------------------------

def upsert_discrepancy(con: sqlite3.Connection, session_id: str, trade_id: str,
                       kind: str, instrument: str | None, detail: dict) -> tuple[int, bool]:
    """Returns (id, created). Second identical call returns created=False and
    the same id -- the UNIQUE constraint is what makes that true, not the agent
    remembering it already flagged this.

    Deliberately a SELECT then a write, rather than one ON CONFLICT statement.
    The clever version detected "was this an insert?" by comparing created_at
    against updated_at, which silently reports an update as an insert whenever
    both calls fall inside the same clock tick -- so it passed by luck and
    failed under test. Two statements that say what they mean are worth more
    here than one that is short."""
    t = now()
    blob = json.dumps(detail, sort_keys=True)
    existing = con.execute(
        "SELECT id FROM discrepancies WHERE session_id = ? AND trade_id = ? AND type = ?",
        (session_id, trade_id, kind)).fetchone()
    if existing:
        con.execute("UPDATE discrepancies SET detail = ?, updated_at = ? WHERE id = ?",
                    (blob, t, existing["id"]))
        con.commit()
        return existing["id"], False
    cur = con.execute(
        """INSERT INTO discrepancies
               (session_id, trade_id, type, instrument, detail, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (session_id, trade_id, kind, instrument, blob, t, t))
    new_id = cur.fetchone()["id"]
    con.commit()
    return new_id, True


def upsert_draft(con: sqlite3.Connection, discrepancy_id: int,
                 body: str) -> tuple[int, str]:
    """Returns (id, outcome) where outcome is created | updated | preserved.

    `preserved` means a human had edited this draft and the agent's version was
    discarded. That is a refusal to overwrite, and it is reported rather than
    raised, because it is a normal outcome and not an error."""
    t = now()
    existing = con.execute("SELECT id, edited FROM drafts WHERE discrepancy_id = ?",
                           (discrepancy_id,)).fetchone()
    if existing and existing["edited"]:
        return existing["id"], "preserved"
    if existing:
        con.execute("UPDATE drafts SET body = ?, updated_at = ? WHERE id = ?",
                    (body, t, existing["id"]))
        con.commit()
        return existing["id"], "updated"
    cur = con.execute(
        """INSERT INTO drafts (discrepancy_id, body, created_at, updated_at)
           VALUES (?, ?, ?, ?) RETURNING id""",
        (discrepancy_id, body, t, t))
    new_id = cur.fetchone()["id"]
    con.commit()
    return new_id, "created"


def idempotency_key(session_id: str, payload: dict) -> str:
    """Stable over payload key order, so the same correction proposed twice in a
    different field order is still the same correction."""
    blob = json.dumps({"session_id": session_id, "payload": payload},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def stage_action(con: sqlite3.Connection, session_id: str, discrepancy_id: int,
                 payload: dict) -> tuple[int, bool]:
    """Insert a PENDING_HUMAN row. Returns (id, created).

    Note what is absent: no status argument. A caller cannot stage something
    already approved, because the column defaults to PENDING_HUMAN and this
    function never writes it."""
    key = idempotency_key(session_id, payload)
    cur = con.execute(
        """INSERT INTO pending_actions
               (idempotency_key, session_id, discrepancy_id, payload, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (idempotency_key) DO NOTHING
           RETURNING id""",
        (key, session_id, discrepancy_id, json.dumps(payload, sort_keys=True), now()))
    row = cur.fetchone()
    con.commit()
    if row:
        return row["id"], True
    existing = con.execute("SELECT id FROM pending_actions WHERE idempotency_key = ?",
                           (key,)).fetchone()
    return existing["id"], False


def log_call(con: sqlite3.Connection, run_id: str, session_id: str, agent_role: str,
             seq: int, tool_name: str, args: dict, ok: bool,
             error: str | None = None) -> None:
    con.execute(
        """INSERT INTO tool_calls
               (run_id, session_id, agent_role, seq, tool_name, args, ok, error, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, session_id, agent_role, seq, tool_name,
         json.dumps(args, sort_keys=True), int(ok), error, now()))
    con.commit()


# --- not a tool --------------------------------------------------------------

def decide_action(con: sqlite3.Connection, action_id: int, approved: bool,
                  decided_by: str) -> bool:
    """Approve or reject a staged action.

    THIS IS THE CAPABILITY GATE. Deliberately not wrapped by any tool in
    tools.py, so it is absent from the agent's list_tools response. The approval
    API in Phase 3 calls it; the agent has no path to it. `decided_by` is
    required, so an approval cannot be anonymous.

    If you ever expose this through MCP, the central claim of this repo stops
    being true. That is why it sits under its own heading.
    """
    cur = con.execute(
        """UPDATE pending_actions
              SET status = ?, decided_at = ?, decided_by = ?
            WHERE id = ? AND status = 'PENDING_HUMAN'""",
        ("APPROVED" if approved else "REJECTED", now(), decided_by, action_id))
    con.commit()
    return cur.rowcount == 1


# --- cli ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    if a.init:
        con = init_db(force=a.force)
        print(f"schema built -> {DB_PATH}")
    else:
        if not DB_PATH.exists():
            raise SystemExit(f"{DB_PATH} does not exist -- run with --init")
        con = connect()

    if a.init or a.stats:
        for t in ("accounts", "sessions", "discrepancies", "drafts",
                  "pending_actions", "tool_calls"):
            n = con.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            print(f"  {t:16s} {n}")


if __name__ == "__main__":
    main()
