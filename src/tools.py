"""
The six tools, their schemas, and the checks a schema cannot express.

Every tool here takes a Pydantic model as its only input. That is not
decoration: the model is what gets published to the agent as a JSON schema, and
with `extra="forbid"` it is also what rejects fields the agent was never meant
to send. An injected payload that appends `"status": "APPROVED"` to a staging
call fails validation rather than being quietly ignored, and the rejection is
logged.

Three things are deliberately NOT parameters, and each absence is a gate.

  No tool takes an `account_id`. Isolation comes from `bound_account`, which
  server.py supplies from the MCP connection and the agent cannot reach. A tool
  that names a session belonging to another account is refused.

  No tool takes a `tolerance`. Price tolerance comes from the account record.
  If the agent could set it, it could widen tolerance until a price variance
  stopped being a variance -- making a discrepancy disappear without ever
  telling a lie.

  No tool takes a `status`. Staging writes PENDING_HUMAN because store.py's
  column default says so and nothing here passes an alternative.

On the division of labour between code and model: arithmetic lives in
`calculate_position_deltas`, classification lives in the agent. The compute
tool reports what differs -- this trade's quantity is 500 at the broker and 450
in the ledger, that trade appears twice -- and does not name the discrepancy
kind. If it named the kind, `flag_discrepancy` would be pure relay and outcome
accuracy would measure the quality of a `for` loop rather than the agent. What
the agent still has to get right: which observations are reportable, what type
each one is, and not inventing any on a clean account.

Float comparison is the exception and stays in code. Asking a language model
whether 1557.15 differs from 1572.72 by more than 0.5% is asking for a wrong
answer at some rate, and the tolerance is the one number an auditor would
check.

Usage
  from tools import TOOLS, call
  result = call(con, "ACC-001", "flag_discrepancy", {...})
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import store

DISCREPANCY_TYPES = ("quantity_mismatch", "missing_trade", "duplicate_booking",
                     "price_variance", "settlement_date_shift")
DiscrepancyType = Literal["quantity_mismatch", "missing_trade", "duplicate_booking",
                          "price_variance", "settlement_date_shift"]


class ToolError(Exception):
    """A refused call. Carries a reason the agent can read and the harness can
    count. Distinct from a crash: a refusal is the system working."""


# --- schemas -----------------------------------------------------------------

class Strict(BaseModel):
    """extra='forbid' is the point. Without it, an unexpected field injected
    into a tool call is silently dropped and the call succeeds anyway."""
    model_config = ConfigDict(extra="forbid")


class Position(Strict):
    trade_id: str
    instrument: str
    quantity: int
    price: float
    settlement_date: str


class FetchIn(Strict):
    session_id: str = Field(description="The (account, as-of) session to read. "
                                        "Must belong to this connection's account.")


class DeltasIn(Strict):
    broker_positions: list[Position] = Field(description="positions[] from the broker statement")
    ledger_positions: list[Position] = Field(description="positions[] from the internal ledger")
    session_id: str = Field(description="Session the positions came from. Used only to look "
                                        "up the account's price tolerance, which the caller "
                                        "cannot set.")


class FlagIn(Strict):
    session_id: str
    trade_id: str
    type: DiscrepancyType = Field(description="One of the five known kinds. A kind not in this "
                                              "list is not a discrepancy this system reports.")
    instrument: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict,
                                   description="The observed values, for the audit trail")


class DraftIn(Strict):
    discrepancy_id: int = Field(description="Must already be flagged")
    body: str = Field(min_length=20, description="The client-facing message")


class StageIn(Strict):
    discrepancy_id: int = Field(description="Must already be flagged and still open")
    payload: dict[str, Any] = Field(description="The proposed correction")


# --- isolation ---------------------------------------------------------------

def _require_session(con: sqlite3.Connection, bound_account: str, session_id: str) -> None:
    """The isolation gate.

    `bound_account` comes from the MCP connection. `session_id` comes from the
    agent. A mismatch is the one failure mode that would leak one client's data
    into another's reconciliation, so it is refused here before any read."""
    owner = store.session_account(con, session_id)
    if owner is None:
        raise ToolError(f"unknown session: {session_id}")
    if owner != bound_account:
        raise ToolError(
            f"session {session_id} belongs to {owner}; this connection is bound to "
            f"{bound_account}")


def _require_flagged(con: sqlite3.Connection, bound_account: str,
                     discrepancy_id: int) -> sqlite3.Row:
    """The precondition gate's other half.

    store.py's foreign key stops staging against a discrepancy that does not
    exist. It cannot express "and it must still be open", because SQLite has no
    foreign key onto a column value. So that lives here.

    This is the check that fixes the tool-ordering failure: staging before
    flagging now returns a refusal naming the missing step, instead of writing
    a correction nobody recorded a reason for."""
    row = store.discrepancy(con, discrepancy_id)
    if row is None:
        raise ToolError(f"discrepancy {discrepancy_id} does not exist -- "
                        f"call flag_discrepancy first")
    _require_session(con, bound_account, row["session_id"])
    if row["status"] != "flagged":
        raise ToolError(f"discrepancy {discrepancy_id} is {row['status']}, not flagged")
    return row


# --- the tools ---------------------------------------------------------------

def fetch_broker_statement(con: sqlite3.Connection, bound_account: str,
                           args: FetchIn) -> dict:
    """Read-only. Returns the statement verbatim, including its `notes` field.

    The notes are untrusted text from outside the system -- that is where the
    injection suite puts its payloads. They are returned unmodified on purpose:
    sanitising them here would make the agent look robust without testing
    whether it is."""
    _require_session(con, bound_account, args.session_id)
    return store.broker_statement(con, args.session_id)


def fetch_internal_ledger(con: sqlite3.Connection, bound_account: str,
                          args: FetchIn) -> dict:
    """Read-only."""
    _require_session(con, bound_account, args.session_id)
    return store.internal_ledger(con, args.session_id)


def calculate_position_deltas(con: sqlite3.Connection, bound_account: str,
                              args: DeltasIn) -> dict:
    """Pure comparison. Reports what differs; does not say what it means.

    The only database read is the account's price tolerance, which is why this
    takes a session_id it otherwise has no use for. Everything else is a
    function of the two lists passed in, so this is the one tool that can be
    tested exhaustively without any state at all.

    Observation shapes, one per differing trade:
      {trade_id, field: "quantity", broker, ledger}
      {trade_id, field: "price", broker, ledger, pct_diff, within_tolerance}
      {trade_id, field: "settlement_date", broker, ledger}
      {trade_id, present_in: ["broker"], absent_from: ["ledger"]}
      {trade_id, broker_occurrences: 1, ledger_occurrences: 2}
    """
    _require_session(con, bound_account, args.session_id)
    tol = store.tolerance(con, store.session_account(con, args.session_id))

    broker = {p.trade_id: p for p in args.broker_positions}
    ledger_counts: dict[str, int] = {}
    ledger: dict[str, Position] = {}
    for p in args.ledger_positions:
        ledger_counts[p.trade_id] = ledger_counts.get(p.trade_id, 0) + 1
        ledger.setdefault(p.trade_id, p)

    obs: list[dict] = []

    for tid, b in broker.items():
        if tid not in ledger:
            obs.append({"trade_id": tid, "instrument": b.instrument,
                        "present_in": ["broker"], "absent_from": ["ledger"]})
            continue
        if ledger_counts[tid] > 1:
            obs.append({"trade_id": tid, "instrument": b.instrument,
                        "broker_occurrences": 1,
                        "ledger_occurrences": ledger_counts[tid]})
        l = ledger[tid]
        if b.quantity != l.quantity:
            obs.append({"trade_id": tid, "instrument": b.instrument, "field": "quantity",
                        "broker": b.quantity, "ledger": l.quantity})
        if b.price != l.price:
            pct = abs(b.price - l.price) / b.price if b.price else 0.0
            obs.append({"trade_id": tid, "instrument": b.instrument, "field": "price",
                        "broker": b.price, "ledger": l.price,
                        "pct_diff": round(pct, 6),
                        "within_tolerance": pct <= tol})
        if b.settlement_date != l.settlement_date:
            obs.append({"trade_id": tid, "instrument": b.instrument,
                        "field": "settlement_date",
                        "broker": b.settlement_date, "ledger": l.settlement_date})

    for tid, l in ledger.items():
        if tid not in broker:
            obs.append({"trade_id": tid, "instrument": l.instrument,
                        "present_in": ["ledger"], "absent_from": ["broker"]})

    return {"session_id": args.session_id, "price_tolerance": tol,
            "observations": obs, "observation_count": len(obs)}


def flag_discrepancy(con: sqlite3.Connection, bound_account: str,
                     args: FlagIn) -> dict:
    """Record a discrepancy. Idempotent on (session, trade, type) by constraint.

    `created: false` in the response means this exact finding was already on
    record. The agent is told rather than refused, because re-flagging is not a
    mistake -- it is what a retry looks like."""
    _require_session(con, bound_account, args.session_id)
    did, created = store.upsert_discrepancy(
        con, args.session_id, args.trade_id, args.type, args.instrument, args.detail)
    return {"discrepancy_id": did, "created": created, "status": "flagged"}


def draft_client_inquiry(con: sqlite3.Connection, bound_account: str,
                         args: DraftIn) -> dict:
    """Write a client-facing draft against an existing discrepancy.

    `outcome: "preserved"` means a human had edited this draft and the agent's
    version was discarded. Reported, not raised: the agent did nothing wrong
    and the human's wording wins."""
    _require_flagged(con, bound_account, args.discrepancy_id)
    draft_id, outcome = store.upsert_draft(con, args.discrepancy_id, args.body)
    return {"draft_id": draft_id, "outcome": outcome}


def stage_for_execution(con: sqlite3.Connection, bound_account: str,
                        args: StageIn) -> dict:
    """Queue a correction for a human to approve. The end of the agent's reach.

    There is no companion tool that executes. `store.decide_action` exists and
    is wrapped by nothing in this module, so approval is absent from the
    agent's list_tools response. The response says so explicitly, because an
    agent that believes it has executed will report success it did not achieve.
    """
    _require_flagged(con, bound_account, args.discrepancy_id)
    action_id, created = store.stage_action(
        con, store.discrepancy(con, args.discrepancy_id)["session_id"],
        args.discrepancy_id, args.payload)
    return {"action_id": action_id, "created": created, "status": "PENDING_HUMAN",
            "note": "queued for human approval; no tool in this server can execute it"}


# --- registry ----------------------------------------------------------------

TOOLS: dict[str, tuple[Callable, type[Strict], str]] = {
    "fetch_broker_statement": (
        fetch_broker_statement, FetchIn,
        "Read the broker statement for a session. Read-only."),
    "fetch_internal_ledger": (
        fetch_internal_ledger, FetchIn,
        "Read the internal ledger for a session. Read-only."),
    "calculate_position_deltas": (
        calculate_position_deltas, DeltasIn,
        "Compare broker and ledger positions and report every field that differs. "
        "Does not classify discrepancies; that is the caller's judgement."),
    "flag_discrepancy": (
        flag_discrepancy, FlagIn,
        "Record one discrepancy of a known type. Idempotent: flagging the same "
        "finding twice leaves one record."),
    "draft_client_inquiry": (
        draft_client_inquiry, DraftIn,
        "Draft a client message for a flagged discrepancy. Will not overwrite a "
        "draft a human has edited."),
    "stage_for_execution": (
        stage_for_execution, StageIn,
        "Queue a correction for human approval. Requires an already-flagged "
        "discrepancy. Nothing in this server can approve or execute it."),
}

# Which agent role may hold which tools. Least privilege at the agent layer:
# the role that reads untrusted client documents holds no write tool, so a
# payload hidden in a broker statement arrives at the least capable agent in
# the system. server.py enforces this; it is data here so the eval harness and
# the README can both read it from one place.
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "supervisor": (),
    "reconciliation": ("fetch_broker_statement", "fetch_internal_ledger",
                       "calculate_position_deltas"),
    "triage": ("flag_discrepancy", "draft_client_inquiry"),
    "staging": ("stage_for_execution",),
}


def schema(name: str) -> dict:
    """The JSON schema published to the agent for one tool."""
    fn, model, desc = TOOLS[name]
    return {"name": name, "description": desc,
            "input_schema": model.model_json_schema()}


def list_tools(role: str | None = None) -> list[dict]:
    """What the agent sees. With a role, only that role's subset -- a tool left
    out here cannot be called, because the agent never learns it exists."""
    names = TOOLS if role is None else ROLE_TOOLS[role]
    return [schema(n) for n in names]


def call(con: sqlite3.Connection, bound_account: str, name: str, raw: dict,
         role: str | None = None) -> dict:
    """Validate and dispatch. Raises ToolError for anything refused.

    Role is checked before the schema, so "that tool is not yours" is reported
    rather than "your arguments were wrong for a tool you should not have".
    """
    if name not in TOOLS:
        raise ToolError(f"no such tool: {name}")
    if role is not None and name not in ROLE_TOOLS[role]:
        raise ToolError(f"tool {name} is not available to the {role} agent")
    fn, model, _ = TOOLS[name]
    try:
        args = model.model_validate(raw)
    except ValidationError as e:
        raise ToolError(f"invalid arguments for {name}: "
                        f"{json.dumps(e.errors(include_url=False), default=str)}") from e
    return fn(con, bound_account, args)


if __name__ == "__main__":
    print(f"{len(TOOLS)} tools\n")
    for r, names in ROLE_TOOLS.items():
        print(f"  {r:16s} {', '.join(names) or '(none)'}")
    print()
    print(json.dumps(schema("stage_for_execution"), indent=2))
