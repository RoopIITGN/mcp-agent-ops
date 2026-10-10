"""
The MCP server. One process per (account, role), bound at launch.

This is where the isolation gate stops being a function argument and becomes a
process boundary. The account and the role are read from argv when the process
starts. Nothing the agent sends over the wire can change either one, because
neither is a parameter of any tool and the agent does not spawn its own server
-- the orchestrator does.

That is also the answer to how this stays safe under parallelism. Fifteen
accounts reconciled at once means fifteen server processes, each bound to one
account. Two workers cannot collide on an account binding because the binding
lives in different processes. A single shared server with an account parameter
would put all fifteen behind one mutable variable, and the isolation argument
would be a comment rather than a fact.

The role binding matters for the same reason. If the agent declared its own
role in a tool call, it could claim to be the staging agent. So the role is
fixed at launch and `list_tools` returns only that role's subset: a tool the
agent is not entitled to is not described to it, does not appear in its
vocabulary, and is refused twice over if it guesses the name.

One finding worth recording, because it cost an hour and would have been
invisible. Tools here take a single `args` object rather than flat keyword
parameters. With flat parameters the SDK derives the schema from the function
signature, strips any argument it does not recognise, and calls the function
with what remains -- so an injected `"status": "APPROVED"` field was dropped
silently and the call SUCCEEDED, with Pydantic never seeing the extra field.
The strictness that tests/test_tools.py verifies at the Python layer did not
survive the protocol boundary. Nesting under `args` restores it: the published
schema carries additionalProperties:false, the model is told the constraint,
and a violation comes back as an error instead of being quietly discarded.
tests/test_server.py asserts this over a real stdio connection, because it is
exactly the kind of thing that regresses on an SDK upgrade.

A key at the JSON-RPC argument root, as a sibling of `args`, is still dropped
by the SDK. That one is harmless: no tool reads anything outside `args`, so a
stray sibling key reaches nothing.

Usage
  python src/server.py --account ACC-001 --role reconciliation
  python src/server.py --account ACC-001 --role triage --run-id run-7
  python src/server.py --describe --role staging      # print the surface, no server
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer                     # noqa: E402
# The SDK's own error type, aliased because tools.ToolError is a different
# thing. Which exception is raised decides whether the agent learns anything:
# the SDK carries the message of a ToolError through to the model ("raised
# deliberately by the tool") and deliberately strips the text of any other
# exception, treating it as a crash whose details stay server-side. A refusal
# raised as a plain ValueError reaches the agent as "Error executing tool
# stage_for_execution" and nothing else, so the agent cannot tell a missing
# precondition from a wrong account and retries the same wrong call.
from mcp.server.mcpserver.exceptions import ToolError as McpToolError  # noqa: E402

import store                                                   # noqa: E402
import tools                                                   # noqa: E402

SERVER_NAME = "mcp-agent-ops"
SERVER_VERSION = "0.2.0"


class Context:
    """What the process knows and the agent cannot tell it."""

    def __init__(self, account: str, role: str, db: Path, run_id: str):
        self.account = account
        self.role = role
        self.run_id = run_id
        self.con = store.connect(db)
        self._seq: dict[str, int] = {}

    def next_seq(self, session_id: str) -> int:
        n = self._seq.get(session_id, 0)
        self._seq[session_id] = n + 1
        return n

    def session_of(self, name: str, raw: dict) -> str:
        """Which session a call belongs to, for the audit log.

        Reads and flags name their session directly. Drafting and staging name
        a discrepancy, so the session is looked up. A call that is about to be
        refused may name nothing resolvable; the log takes an empty string
        rather than failing, because a refused call still has to be recorded."""
        if isinstance(raw.get("session_id"), str):
            return raw["session_id"]
        did = raw.get("discrepancy_id")
        if isinstance(did, int):
            row = store.discrepancy(self.con, did)
            if row is not None:
                return row["session_id"]
        return ""


def _wrap(ctx: Context, name: str):
    """Build the published tool function for one tool.

    The single `args` parameter is deliberate -- see the module docstring. The
    signature is set explicitly because the SDK reads inspect.signature, which
    does not pick up a reassigned __annotations__ on its own.

    `impl` is async for a reason that is not style: the SDK runs a synchronous
    tool function in a worker thread, and a sqlite3 connection may only be used
    in the thread that created it. A sync tool here fails with
    "SQLite objects created in a thread can only be used in that same thread"
    on the first call. Making it async keeps every database call on the event
    loop in the process's own thread, so one connection serves the process and
    calls serialise -- which is what we want anyway, since this process serves
    one account and one agent.
    """
    _, model, desc = tools.TOOLS[name]

    async def impl(args):
        raw = args.model_dump()
        session_id = ctx.session_of(name, raw)
        seq = ctx.next_seq(session_id)
        try:
            out = tools.call(ctx.con, ctx.account, name, raw, role=ctx.role)
        except tools.ToolError as e:
            store.log_call(ctx.con, ctx.run_id, session_id, ctx.role, seq,
                           name, raw, ok=False, error=str(e))
            # McpToolError, not ValueError: a refusal is the system working and
            # the agent has to be able to read why, or it cannot choose a
            # different next step. See the import comment.
            raise McpToolError(str(e)) from e
        store.log_call(ctx.con, ctx.run_id, session_id, ctx.role, seq,
                       name, raw, ok=True)
        return out

    impl.__name__ = name
    impl.__doc__ = desc
    impl.__signature__ = inspect.Signature(
        [inspect.Parameter("args", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           annotation=model)])
    impl.__annotations__ = {"args": model, "return": dict}
    return impl


def build(account: str, role: str, db: Path, run_id: str) -> tuple[MCPServer, Context]:
    """Assemble a server holding exactly one role's tools.

    Returned alongside its Context so tests can drive it in-process. The
    subprocess path in main() uses the same function, so what the tests
    exercise is what ships."""
    if role not in tools.ROLE_TOOLS:
        raise SystemExit(f"unknown role {role!r}; expected one of "
                         f"{', '.join(tools.ROLE_TOOLS)}")

    ctx = Context(account, role, db, run_id)
    srv = MCPServer(
        name=f"{SERVER_NAME}:{role}",
        version=SERVER_VERSION,
        instructions=(
            f"Reconciliation tools for account {account}, {role} role. "
            f"Every tool takes one object argument named 'args'. "
            f"Tools outside this role's set are not available and no tool in "
            f"this server can approve or execute anything."),
    )
    for name in tools.ROLE_TOOLS[role]:
        srv.add_tool(_wrap(ctx, name), description=tools.TOOLS[name][2])
    return srv, ctx


def describe(role: str) -> None:
    """Print the surface without starting a server -- what the agent would see."""
    names = tools.ROLE_TOOLS[role]
    print(f"role {role}: {len(names)} tool(s)")
    for n in names:
        print(f"  {n}")
    print()
    if names:
        print(json.dumps(tools.schema(names[0]), indent=2))
    missing = sorted(set(tools.TOOLS) - set(names))
    print(f"\nnot visible to this role: {', '.join(missing) or '(none)'}")
    print("never visible to any role: approval, execution")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--account", help="account this process is bound to, e.g. ACC-001")
    ap.add_argument("--role", required=True, choices=sorted(tools.ROLE_TOOLS))
    ap.add_argument("--db", default=str(store.DB_PATH))
    ap.add_argument("--run-id", default=os.environ.get("RUN_ID", ""))
    ap.add_argument("--describe", action="store_true",
                    help="print this role's tool surface and exit")
    a = ap.parse_args()

    if a.describe:
        describe(a.role)
        return

    if not a.account:
        raise SystemExit("--account is required (it is the isolation boundary)")

    db = Path(a.db)
    if not db.exists():
        raise SystemExit(f"{db} does not exist -- run: python src/store.py --init")

    run_id = a.run_id or f"run-{uuid.uuid4().hex[:8]}"
    srv, _ = build(a.account, a.role, db, run_id)
    # stdout is the protocol channel on stdio transport. Anything printed to it
    # corrupts the stream, which is why every message in this file goes to
    # stderr or through the server's own logging.
    print(f"{SERVER_NAME}:{a.role} bound to {a.account}, run {run_id}", file=sys.stderr)
    srv.run("stdio")


if __name__ == "__main__":
    main()
