"""
The gates, asserted.

Every claim this repo makes about structural safety is a claim about behaviour
under a specific wrong call: the agent flags the same thing twice, stages
against a discrepancy that was never flagged, retries a staging call, or tries
to overwrite a draft a human has edited. Those are the cases here.

The point of writing them as tests rather than checking once by hand is that a
gate can be removed by accident. Dropping `PRAGMA foreign_keys = ON` from
store.connect() would disable the precondition gate completely and every other
test in the project would still pass. This file is what fails.

Each test gets its own throwaway database, so running the suite never touches
data/ops.db. A test that mutates the real database is a test you stop running.

Usage
  python tests/test_gates.py        # zero dependencies
  pytest tests/test_gates.py        # also works
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import store                                                   # noqa: E402

SESSION = "ACC-001-2026-09-01"


def fresh() -> sqlite3.Connection:
    """A database with the real schema and the real fixtures, in a temp file."""
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    return store.init_db(force=True, path=tmp)


def a_discrepancy(con: sqlite3.Connection) -> int:
    did, _ = store.upsert_discrepancy(
        con, SESSION, "T794772", "settlement_date_shift", "BHARTIARTL",
        {"broker_settlement": "2026-09-03", "ledger_settlement": "2026-09-04"})
    return did


# --- idempotency gate --------------------------------------------------------

def test_flagging_twice_is_one_row():
    con = fresh()
    i1, created1 = store.upsert_discrepancy(con, SESSION, "T1", "quantity_mismatch", "INFY", {"q": 1})
    i2, created2 = store.upsert_discrepancy(con, SESSION, "T1", "quantity_mismatch", "INFY", {"q": 1})
    assert i1 == i2, "same finding produced two ids"
    assert created1 and not created2, "second call reported itself as a new row"
    n = con.execute("SELECT count(*) AS n FROM discrepancies").fetchone()["n"]
    assert n == 1, f"expected 1 row, found {n}"


def test_staging_twice_is_one_action():
    con = fresh()
    did = a_discrepancy(con)
    payload = {"correction": "set settlement_date to 2026-09-03"}
    a1, created1 = store.stage_action(con, SESSION, did, payload)
    a2, created2 = store.stage_action(con, SESSION, did, payload)
    assert a1 == a2 and created1 and not created2
    n = con.execute("SELECT count(*) AS n FROM pending_actions").fetchone()["n"]
    assert n == 1, f"a retry created {n} pending actions"


def test_idempotency_key_ignores_field_order():
    assert (store.idempotency_key(SESSION, {"a": 1, "b": 2})
            == store.idempotency_key(SESSION, {"b": 2, "a": 1}))


def test_idempotency_key_separates_different_payloads():
    assert (store.idempotency_key(SESSION, {"a": 1})
            != store.idempotency_key(SESSION, {"a": 2}))


# --- precondition gate -------------------------------------------------------

def test_cannot_stage_against_unflagged_discrepancy():
    """The ordering failure seen in production: staging before flagging.

    This is the test that fails if foreign keys are ever left off."""
    con = fresh()
    try:
        store.stage_action(con, SESSION, 99999, {"correction": "anything"})
    except sqlite3.IntegrityError:
        return
    raise AssertionError("staged an action against a discrepancy that does not exist")


def test_cannot_stage_against_unknown_session():
    con = fresh()
    did = a_discrepancy(con)
    try:
        store.stage_action(con, "ACC-999-2026-01-01", did, {"correction": "x"})
    except sqlite3.IntegrityError:
        return
    raise AssertionError("staged an action for a session that does not exist")


# --- capability gate ---------------------------------------------------------

def test_staged_actions_start_pending_and_anonymous():
    con = fresh()
    did = a_discrepancy(con)
    aid, _ = store.stage_action(con, SESSION, did, {"correction": "x"})
    row = con.execute("SELECT status, decided_at, decided_by FROM pending_actions WHERE id = ?",
                      (aid,)).fetchone()
    assert row["status"] == "PENDING_HUMAN"
    assert row["decided_at"] is None and row["decided_by"] is None


def test_approval_is_single_use():
    """Replaying an approval must not re-approve. Protects against a retried
    approval request being treated as a second authorisation."""
    con = fresh()
    did = a_discrepancy(con)
    aid, _ = store.stage_action(con, SESSION, did, {"correction": "x"})
    assert store.decide_action(con, aid, True, "ops@firm") is True
    assert store.decide_action(con, aid, True, "ops@firm") is False
    assert store.decide_action(con, aid, False, "other@firm") is False
    row = con.execute("SELECT status, decided_by FROM pending_actions WHERE id = ?",
                      (aid,)).fetchone()
    assert row["status"] == "APPROVED" and row["decided_by"] == "ops@firm"


def test_status_column_rejects_invented_states():
    con = fresh()
    did = a_discrepancy(con)
    aid, _ = store.stage_action(con, SESSION, did, {"correction": "x"})
    try:
        con.execute("UPDATE pending_actions SET status = 'EXECUTED' WHERE id = ?", (aid,))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("CHECK constraint allowed a status outside the three legal values")


def test_no_tool_wraps_decide_action():
    """The capability gate is only real while tools.py does not expose approval.

    Skipped until tools.py exists; becomes a real assertion then. Left here so
    the omission is tracked by the suite rather than by memory."""
    tools_py = Path(__file__).resolve().parents[1] / "src/tools.py"
    if not tools_py.exists():
        print("    (skipped: src/tools.py not written yet)")
        return
    src = tools_py.read_text(encoding="utf-8")
    assert "decide_action" not in src, \
        "src/tools.py references decide_action -- the capability gate is broken"


# --- draft preservation ------------------------------------------------------

def test_agent_cannot_overwrite_a_human_edited_draft():
    con = fresh()
    did = a_discrepancy(con)
    d1, out1 = store.upsert_draft(con, did, "agent draft one")
    d2, out2 = store.upsert_draft(con, did, "agent draft two")
    assert (out1, out2) == ("created", "updated") and d1 == d2

    con.execute("UPDATE drafts SET edited = 1 WHERE id = ?", (d1,))
    con.commit()

    d3, out3 = store.upsert_draft(con, did, "agent tries again")
    assert out3 == "preserved", f"expected preserved, got {out3}"
    body = con.execute("SELECT body FROM drafts WHERE id = ?", (d1,)).fetchone()["body"]
    assert body == "agent draft two", f"human edit was overwritten: {body!r}"


# --- isolation helper --------------------------------------------------------

def test_session_account_is_authoritative():
    con = fresh()
    assert store.session_account(con, SESSION) == "ACC-001"
    assert store.session_account(con, "ACC-999-2026-01-01") is None


# --- fixtures ----------------------------------------------------------------

def test_truth_is_not_in_the_database():
    """If the agent's database could see the seeded answers, every number
    measured against it would be meaningless."""
    con = fresh()
    tables = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
    assert not any("truth" in t for t in tables), f"truth leaked into schema: {tables}"
    blob = " ".join(json.dumps(dict(r)) for r in
                    con.execute("SELECT * FROM sessions").fetchall())
    assert "discrepanc" not in blob.lower(), "session rows name discrepancies"


def test_fixtures_loaded():
    con = fresh()
    assert con.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 15
    assert con.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 60


# --- runner ------------------------------------------------------------------

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
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)} tests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
