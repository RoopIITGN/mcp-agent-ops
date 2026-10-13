"""
What the supervisor is allowed to say, and what it is allowed to see.

The supervisor is an orchestrator and nothing else. It decides one step at a
time -- invoke a worker, or stop -- and it never touches the books. This file is
where both halves of that are made structural rather than hoped for: `Decision`
constrains what it can emit, and `WorkerOutcome` constrains what it can read.

It holds zero MCP tools, and that claim stays literally true. Its one output
channel is a function call named `submit_decision`, which is not in `tools.TOOLS`,
is not served by any MCP server, and cannot read or write the database. It is how
a typed answer leaves a language model, not a capability. `assert_output_channel_
is_not_a_tool()` is the gate; if an interviewer presses on "so it does have a
tool", the answer is that it has an output schema, not an ability.

The six gates, and where each one lives

  1 decision validity      `Literal` + extra="forbid". An invented role, an
                           invented field or a free-text action fails to parse,
                           before any worker process is started.

  2 no privilege escalation a decision names a ROLE, never a tool. ROLE_TOOLS
                           stays fixed and is bound at process launch, so there
                           is no code path by which a decision widens a worker's
                           reach. Asserted by the absence of a tool field here
                           and by a test over this module's schema.

  3 account binding        `bind_check` compares the decision's client_id with
                           the query's. The supervisor cannot route itself into
                           another client's book.

  4 step cap               `check_cap`: at most five worker invocations per
                           query, at most two of any one role. Code, not a
                           sentence in a prompt, because an unbounded
                           re-planning loop is the obvious way this design burns
                           a budget.

  5 context boundary       `WorkerOutcome.render` emits counts and redacted
                           refusal text. `assert_no_book_data` is the assertion.
                           See the note on redaction below -- it is the one place
                           where this gate nearly leaked.

  6 exactly one action     a model validator. A decision that both invokes and
                           refuses is not a decision.

On redaction, which is where this almost went wrong

The context boundary is easy to state and easy to get wrong, because refusal
text is written for the agent that was refused, not for the supervisor. The
staging gate's own words are "discrepancy 45 is on trade T570760, not T111111" --
a trade id, handed straight to the component that is supposed to never see the
books. It would not let the supervisor recompute anything, since it still cannot
read a statement, but "mostly cannot see the books" is not a gate.

So refusal text is redacted before the supervisor sees it: trade ids and decimal
figures become placeholders. The supervisor learns THAT a precondition failed and
WHICH one, which is all it needs to decide whether to re-plan. It does not learn
which trade or which price, which is what it has no business knowing.

Usage
  from plan import Decision, WorkerOutcome, bind_check, check_cap
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

import tools
from tools import Strict

# --- the role vocabulary ------------------------------------------------------
#
# Spelled out as a Literal because that is what makes an invented role a parse
# error rather than a runtime surprise. Spelled out means it can drift from
# tools.ROLE_TOOLS, so the assertion below runs at import: if a role is ever
# added or renamed there and not here, every import of this module fails loudly
# instead of the supervisor quietly losing the ability to route to it.
Role = Literal["reconciliation", "triage", "staging"]
ROLES: tuple[str, ...] = ("reconciliation", "triage", "staging")

Action = Literal["invoke", "done", "refuse", "clarify"]

MAX_WORKER_CALLS = 5      # a query needing more has lost the thread
MAX_PER_ROLE = 2          # one retry per role: enough to recover from a gate
                          # firing, not enough to loop

# A separate, larger cap on model calls. The worker cap alone does not bound the
# loop: a supervisor whose every decision is REJECTED invokes no worker, so the
# worker count never rises and it would spin forever. A stub that emits an
# invented role does exactly that, which is how this was found.
MAX_SUPERVISOR_CALLS = 10

OUTPUT_CHANNEL = "submit_decision"

#: The clock the supervisor resolves dates against. Global and public: it is not
#: client data, so unlike a per-client statement calendar there is nothing here
#: that could be paired with the wrong request.
BUSINESS_DATE = "2026-09-25"

_worker_roles = tuple(r for r in tools.ROLE_TOOLS if r != "supervisor")
assert set(ROLES) == set(_worker_roles), (
    f"plan.ROLES {ROLES} has drifted from tools.ROLE_TOOLS {_worker_roles}")
assert tools.ROLE_TOOLS["supervisor"] == (), (
    "the supervisor must hold no tools")


# --- what the supervisor knows before anything has run ------------------------
#
# Found by auditing every decision the ten intents require against what the
# supervisor actually receives. Counts and refusal text cover the continue-or-
# stop decisions, but two whole classes were undecidable without this block:
#
#   refusing out of scope (18 of 68 queries: "approve and execute the pending
#   corrections", "what's the P&L impact", "also check ACC-005"). The supervisor
#   cannot know a request is impossible unless it knows what the system can do.
#
#   resolving a date. The corpus writes dates as "the 8th", "8 Sep", "Sep 8".
#   None of those resolve without a calendar, and the supervisor was being asked
#   to emit an ISO date from thin air.
#
# Neither breaches the boundary. A capability list is not a ledger, and a list of
# statement dates is a calendar, not positions. The supervisor still cannot find
# out whether a date's books agree without sending a worker to look -- which is
# exactly the property the early-stop class measures.

ROLE_BRIEF: dict[str, str] = {
    # OBSERVED IS NOT RECORDED, and these three briefs now say so three times.
    #
    # A measured failure, and the words caused it. A supervisor read
    # "reconciliation found 1 observation" as meaning a discrepancy had been
    # recorded, skipped triage, and called staging -- which found nothing
    # recorded, queued nothing, and reported "0 corrections were queued" on a
    # session holding a genuine 3.4% price break. The old staging brief did say
    # "needs the discrepancy to be already recorded"; it never said that
    # reconciliation does not record, so "observation" and "recorded
    # discrepancy" read as the same state.
    #
    # Three states, named in the same words every time they appear:
    #   reconciliation -> OBSERVATIONS   (nothing is recorded)
    #   triage         -> RECORDED DISCREPANCIES
    #   staging        -> needs a RECORDED discrepancy, not an observation
    "reconciliation":
        "Reads one client's broker statement and internal ledger for one as-of "
        "date and reports every field that differs. What it returns are "
        "OBSERVATIONS. Read-only: it records nothing and changes nothing, so an "
        "observation is not yet a recorded discrepancy. Also surfaces the "
        "counterparty's free-text note.",
    "triage":
        "The only worker that RECORDS. Takes reconciliation's observations, "
        "classifies each into one of the five known discrepancy kinds, records "
        "them, and drafts one client message per discrepancy. Needs "
        "reconciliation to have run first. Cannot queue a correction.",
    "staging":
        "Queues one correction per RECORDED discrepancy for a human to approve. "
        "It needs triage to have recorded the discrepancy first: an observation "
        "from reconciliation is NOT a recorded discrepancy, and staging called "
        "before triage finds nothing to queue and queues nothing. So staging is "
        "never the step straight after reconciliation, and never the first step "
        "on a date nobody has reconciled.",
}

# What no role can do, which is what makes an out-of-scope request refusable
# rather than something to attempt and fail at. The first line is asserted
# against the tool registry below, so it cannot become false quietly.
SYSTEM_CANNOT: tuple[str, ...] = (
    # The line that caused a wrong refusal, and the contrast it was missing.
    #
    # A real model read "queue the correction" in a client request, matched it
    # against this line's verbs, and refused -- "this system has no capability
    # to approve, execute, apply, or release corrections". Queuing is the one
    # thing staging exists to do. The list said what is forbidden without
    # saying that the adjacent, legitimate step is not, so the model extended
    # the prohibition by one step.
    "approve, execute, apply or release a correction -- no tool exists for it, "
    "in any role; a queued correction waits for a human. Queuing one for a "
    "human to approve is NOT in this list: that "
    "is the staging worker's job and the last step this system performs. A "
    "request to queue, stage or put a correction in the approval queue is in "
    "scope; only approving or carrying it out afterwards is not",
    "value positions, or compute P&L, mark-to-market, margin or tax",
    "read or write any client other than the one this request came from",
    "change a price tolerance, or decide a difference is acceptable",
)

_FORBIDDEN_VERBS = ("approve", "execute", "decide", "confirm", "settle", "release")

assert set(ROLE_BRIEF) == set(ROLES), (
    f"ROLE_BRIEF {sorted(ROLE_BRIEF)} has drifted from ROLES {sorted(ROLES)}; a "
    f"role with no brief is a role the supervisor cannot reason about")
assert not [n for n in tools.TOOLS for v in _FORBIDDEN_VERBS if v in n], (
    "SYSTEM_CANNOT claims nothing can approve or execute, but a tool name now "
    "contains one of those verbs")


def capabilities_brief() -> str:
    """The role catalogue, rendered for the supervisor's system prompt.

    Built from `tools.ROLE_TOOLS` rather than hand-written, so a role whose
    grants change cannot end up described by a stale paragraph. The tool names
    are included because the supervisor reasoning about "can this be done at
    all" is more reliable when it can see the vocabulary, and naming a tool is
    not the same as being able to call one -- it holds none.
    """
    lines = ["The three workers you may invoke, one per decision:"]
    for role in ROLES:
        granted = ", ".join(tools.ROLE_TOOLS[role])
        lines.append(f"\n  {role}")
        lines.append(f"    {ROLE_BRIEF[role]}")
        lines.append(f"    its tools: {granted}")
    lines.append("\nYou hold no tools yourself and cannot read any client's "
                 "books. The workers do that and report counts back to you.")
    lines.append("\nThis system cannot:")
    for item in SYSTEM_CANNOT:
        lines.append(f"  - {item}")
    return "\n".join(lines)


class CapExceeded(Exception):
    """The step cap fired. A refusal of the orchestrator, not of a worker."""


class BindingViolation(Exception):
    """A decision named a client other than the one that asked. The isolation
    gate, moved up a layer: it used to only be reachable at the tool boundary,
    and an orchestrator that can choose accounts needs it one level higher."""


# --- what the supervisor may emit ---------------------------------------------

class Decision(Strict):
    """One step. Not a plan -- a plan commits before the books have been read.

    There is deliberately no `tool` field and no `tools` field. The supervisor
    picks a role; what that role may call is `tools.ROLE_TOOLS`, fixed in code
    and bound into the worker process at launch. That absence is gate 2, and a
    test asserts this schema never grows such a field.

    There is also no `tolerance`, no `status` and no `account_id` beyond
    `client_id`, for the same reasons those are absent from every tool schema.
    """

    action: Action = Field(
        description="invoke: run one worker now. done: the request is served. "
                    "refuse: it cannot be served at all. clarify: it cannot be "
                    "served as written and a question has to go back.")
    role: Role | None = Field(
        default=None,
        description="Required when action is invoke, and forbidden otherwise. "
                    "One role per decision: the point of deciding each step is "
                    "seeing the previous outcome first.")
    client_id: str = Field(
        description="The client this request belongs to. Must be the client that "
                    "asked; naming another is refused.")
    session_date: str | None = Field(
        default=None,
        description="The as-of date, ISO (YYYY-MM-DD), resolved from however the "
                    "request phrased it. Null only when clarifying.")
    reason: str = Field(
        min_length=15, max_length=400,
        description="Why this step, in one sentence, for the audit trail.")
    message: str | None = Field(
        default=None,
        description="Required for refuse and clarify: what goes back to the "
                    "client. Optional on done as a closing summary.")
    declined: list[str] = Field(
        default_factory=list, max_length=4,
        description="Parts of the request not served -- another client's book, a "
                    "capability this system does not have. A request can be "
                    "served and still have a declined part.")

    @model_validator(mode="after")
    def _one_coherent_action(self):
        """Gate 6, plus gate 1's half that a type cannot express."""
        if self.action == "invoke":
            if self.role is None:
                raise ValueError("action 'invoke' requires a role")
            if self.message:
                raise ValueError(
                    "action 'invoke' takes no client message; invoking a worker "
                    "is not a reply to the client")
        else:
            if self.role is not None:
                raise ValueError(
                    f"action '{self.action}' must not name a role; a role is only "
                    f"named when invoking one")
        if self.action == "invoke" and not self.session_date:
            raise ValueError(
                "action 'invoke' requires a session_date: a worker reads one "
                "client's books for one as-of date, and there is no default")
        if self.action in ("refuse", "clarify") and not (self.message or "").strip():
            raise ValueError(f"action '{self.action}' requires a message to the client")
        # There was a rule here rejecting `clarify` with a session_date, on the
        # reasoning that a known date leaves nothing to clarify. It was wrong,
        # and it was the single largest source of failure in the first properly
        # measured run: 69 of 287 decisions rejected, 24%, every one of them
        # this rule and nothing else.
        #
        # It is wrong twice over. A clarification need not be about the date at
        # all -- "I can reconcile ACC-015 for 22 Sep, do you also want ACC-005?"
        # is a clarify with a perfectly good date. And where the clarification
        # IS about the date, naming the one it tried is what makes the question
        # answerable: "there is no statement for 2026-09-25, which date did you
        # mean?" beats "which date did you mean?" by a wide margin.
        #
        # The rule forced the model to throw away the fact the client needed,
        # then rejected it for complying. Lesson recorded rather than quietly
        # deleted: a coherence rule invented at the desk, with no example
        # behind it, cost a quarter of a paid run.
        return self

    @property
    def is_terminal(self) -> bool:
        return self.action != "invoke"


def decision_tool_schema() -> dict:
    """The supervisor's single output channel, as a function schema.

    Named `submit_decision`, and it is not an MCP tool: nothing serves it, it
    reads nothing and writes nothing. The model calls it the way it would call a
    tool because that is the only typed output channel the API offers, and
    reusing it means `extra="forbid"` is enforced on the same path that already
    works for the six real tools.
    """
    return {
        "name": OUTPUT_CHANNEL,
        "description": (
            "Record the next orchestration step. Invoke exactly one worker, or "
            "stop with done, refuse or clarify. You hold no tools of your own and "
            "cannot read any client's books; the workers do that and report back."),
        "input_schema": Decision.model_json_schema(),
    }


def assert_output_channel_is_not_a_tool() -> None:
    """Gate 2, asserted rather than asserted-in-prose.

    `submit_decision` must not be in the tool registry and must not appear in
    any role's grant. If it ever did, the supervisor would have acquired a
    capability and the README's central claim would be false.
    """
    if OUTPUT_CHANNEL in tools.TOOLS:
        raise AssertionError(
            f"{OUTPUT_CHANNEL} is in tools.TOOLS -- the supervisor's output "
            f"channel has become a real tool")
    for role, granted in tools.ROLE_TOOLS.items():
        if OUTPUT_CHANNEL in granted:
            raise AssertionError(f"{OUTPUT_CHANNEL} granted to {role}")


def assert_decision_grants_no_tools() -> None:
    """Gate 2's other half: the schema must offer no way to name a tool.

    A field called `tool`, `tools`, `allow` or `grant` would let the supervisor
    widen a worker's reach, which is the one thing an orchestrator must not be
    able to do. Checked against the generated JSON schema rather than the class,
    so an alias or a computed field cannot sneak past.
    """
    forbidden = {"tool", "tools", "tool_name", "allow", "allowed_tools", "grant",
                 "grants", "permissions", "tolerance", "status", "account_id"}
    present = set(Decision.model_json_schema()["properties"])
    bad = present & forbidden
    if bad:
        raise AssertionError(f"Decision exposes {sorted(bad)}")


# --- gates 3 and 4 ------------------------------------------------------------

def bind_check(decision: Decision, query_client: str) -> None:
    """Gate 3. The decision's client must be the client that asked.

    Deliberately a refusal and not a silent correction. Overwriting the field
    with the right value would make the system behave correctly and measure
    nothing: a supervisor that tried to reach another book would look identical
    to one that never tried.
    """
    if decision.client_id != query_client:
        raise BindingViolation(
            f"decision names client {decision.client_id}; this request came from "
            f"{query_client}")


def check_cap(invoked: list[str], role: str) -> None:
    """Gate 4. Raises before the invocation, so the cap is never exceeded.

    `invoked` is the roles invoked so far for this query, in order.
    """
    if len(invoked) >= MAX_WORKER_CALLS:
        raise CapExceeded(
            f"already invoked {len(invoked)} workers for this request "
            f"(cap {MAX_WORKER_CALLS}): {', '.join(invoked)}")
    used = invoked.count(role)
    if used >= MAX_PER_ROLE:
        raise CapExceeded(
            f"{role} has already run {used} time(s) (cap {MAX_PER_ROLE}); "
            f"re-running it again will not change the outcome")


# --- gate 5: what the supervisor may see --------------------------------------

_TRADE = re.compile(r"\bT\d{4,}\b")
_DECIMAL = re.compile(r"\b\d+\.\d+\b")
_BIG_INT = re.compile(r"\b\d{3,}\b")       # quantities; discrepancy ids are small

# Identifiers the supervisor is entitled to and must not lose. An account id and
# an as-of date are routing information, not book data: the supervisor chose
# them, and a refusal that came back as "unknown session ACC-<num>-<num>-09-08"
# would tell it nothing about which of its own decisions failed. Protected ahead
# of redaction because both contain digit runs that the quantity rule would eat.
_KEEP = re.compile(r"\bACC-\d{3}(?:-\d{4}-\d{2}-\d{2})?\b|\b\d{4}-\d{2}-\d{2}\b")

#: Anything matching this in supervisor-bound text is book data that leaked.
#: Deliberately narrower than the redactor: account ids and dates are legitimate.
LEAK = re.compile("|".join(p.pattern for p in (_TRADE, _DECIMAL)))


def _token(i: int) -> str:
    """Letters, not digits. A numeric placeholder would itself be caught by the
    quantity rule once the index reached three digits."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return f"\x00{s}\x00"


def redact(text: str) -> str:
    """Strip book data from text written for a worker before a supervisor reads it.

    Refusal messages are written for the agent that was refused: the staging
    gate says "discrepancy 45 is on trade T570760, not T111111". That is the
    right message for a worker and the wrong one for an orchestrator, which is
    supposed to know that a precondition failed and which one, not which trade.

    Order matters, and getting it wrong is silent. Account ids and dates are
    stashed first because the quantity rule would eat their digit runs and leave
    the supervisor unable to tell which of its own decisions a refusal refers
    to. Then trade ids (before the integer rule, or `T570760` is half-eaten and
    stops reading as a redaction), then decimals, then long integers.
    """
    kept: list[str] = []

    def stash(m: re.Match) -> str:
        kept.append(m.group(0))
        return _token(len(kept) - 1)

    text = _KEEP.sub(stash, text)
    text = _TRADE.sub("<trade>", text)
    text = _DECIMAL.sub("<num>", text)
    text = _BIG_INT.sub("<num>", text)
    for i, original in enumerate(kept):
        text = text.replace(_token(i), original)
    return text


def assert_no_book_data(text: str) -> None:
    """Gate 5. Raises if supervisor-bound text carries a trade id or a figure."""
    hit = LEAK.search(text)
    if hit:
        raise AssertionError(
            f"supervisor context leaked book data: {hit.group(0)!r} in {text[:160]!r}")


class WorkerOutcome(Strict):
    """What comes back from a worker. Built by code, never by a model.

    Counts and refusal reasons. No positions, no prices, no quantities, no trade
    ids. The supervisor decides WHETHER to continue; the orchestration code
    decides WHAT to pass to the next worker, which it already does by threading
    triage's findings into staging's task.

    Three things follow from that, and the third is the one that makes it a gate
    rather than an intention: the supervisor cannot redo a worker's work because
    it never receives the inputs to redo it with; its context stays small, so
    re-planning stays cheap; and `render` is testable.
    """

    role: Role
    tools_called: list[str] = Field(default_factory=list)
    observations: int = Field(default=0, ge=0,
                              description="reconciliation: fields that differ")
    flagged: int = Field(default=0, ge=0, description="triage: discrepancies recorded")
    staged: int = Field(default=0, ge=0, description="staging: corrections queued")
    refusals: list[str] = Field(default_factory=list,
                                description="gate messages, redacted")
    stopped: Literal["done", "cap", "backend_error"] = "done"
    #: Did any tool return data at all? Computed in code, never by a model.
    #:
    #: The most dangerous output this system produced came from the absence of
    #: this flag. A request naming no date was resolved to the business date,
    #: which has no statement loaded; all three read tools answered "unknown
    #: session"; and the count was, correctly, zero observations. The supervisor
    #: was handed "0 observations" alongside three refusals, joined them wrongly,
    #: and told the client "no differences were found for ACC-010" -- a clean
    #: bill of health on books that were never opened. A refusal is safe and a
    #: wrong date is recoverable; a false all-clear closes the ticket.
    #:
    #: The information was always present: the count and the refusals sit side by
    #: side. Nothing said the count was void when the refusals were total, so the
    #: model had to infer it, and in one of two cases it did not. This flag makes
    #: that inference unnecessary -- `render` refuses to print a count as a
    #: finding when nothing was read. Enforced at the boundary, not asked for in
    #: the instruction.
    #:
    #: MEASURED, over two arms of 80: requests ending `done` with every read
    #: refused fell from 23 to 3, and not one of the three claimed the books
    #: agreed. The false all-clear is gone.
    #:
    #: The first version of this text cost eight points of fully-correct, and the
    #: reason is worth keeping. It ended "do not report this as a result to the
    #: client", which is right when the supervisor picked the date itself and
    #: wrong when the client named it -- a client who asks about 29 September is
    #: entitled to hear that no statement is loaded for it. One request class
    #: went from 100% to 0% on that sentence alone. Having just argued that a
    #: structural fix beats an instruction, I attached an instruction to a
    #: structural fix and it overshot in the one direction the structure could
    #: not see. The structure stands; the sentence now splits the two cases.
    read_ok: bool = Field(
        default=True,
        description="at least one tool returned data rather than a refusal")

    def render(self) -> str:
        """The lines the supervisor sees. Redacted, and asserted to be clean.

        The assertion is inside the renderer on purpose. A gate that is checked
        by a test but not by the code it guards is a gate that holds until
        someone adds a field.
        """
        counts = {"reconciliation": ("observations", self.observations),
                  "triage": ("discrepancies recorded", self.flagged),
                  "staging": ("corrections queued", self.staged)}
        label, value = counts[self.role]
        if not self.read_ok:
            # No count is printed at all. A zero rendered beside its refusals is
            # a number a reader can quote; the absence of one is not.
            parts = [f"{self.role}: NOTHING WAS READ. Every tool call was "
                     f"refused, so this worker has no finding of any kind. "
                     f"There is no count below because a count of zero here "
                     f"would mean the books agree, and the books were never "
                     f"opened. Which answer this leads to depends on where the "
                     f"date came from. If the client named it, that no statement "
                     f"is loaded for that date IS the answer: say so and stop. "
                     f"If you chose the date yourself, you have no answer at "
                     f"all, and the client has to be asked which date was "
                     f"meant.",
                     f"  tools used: {', '.join(self.tools_called) or 'none'}",
                     f"  finished: {self.stopped}"]
        else:
            parts = [f"{self.role}: {value} {label}",
                     f"  tools used: {', '.join(self.tools_called) or 'none'}",
                     f"  finished: {self.stopped}"]
        for r in self.refusals:
            parts.append(f"  REFUSED: {redact(r)}")
        text = "\n".join(parts)
        assert_no_book_data(text)
        return text

    @property
    def produced_work(self) -> bool:
        """Whether there is anything for a later worker to act on. The signal
        the supervisor needs to decide 'done' on a session whose books agree --
        and the whole reason deciding each step beats deciding once.

        `read_ok` gates it, because a session nothing could be read from has
        produced no work and also settled nothing -- the two are different and
        only the second was ever in doubt."""
        return bool(self.read_ok
                    and (self.observations or self.flagged or self.staged))


class GateRejection(Strict):
    """A decision the orchestration layer refused before any worker ran.

    The third gap the audit found. `bind_check` and `check_cap` raise, and a
    raised exception produces no `WorkerOutcome` -- so a supervisor whose
    decision was rejected would see an unchanged history, conclude its last
    decision had not been made yet, and emit it again. It would do that until
    the cap stopped it, and the transcript would show a model stuck in a loop
    when in fact it was never told it had been blocked.

    So a rejection is a history entry in its own right, and it names the gate.
    """

    gate: Literal["account_binding", "step_cap", "decision_invalid"]
    attempted_action: str
    attempted_role: str | None = None
    detail: str

    def render(self) -> str:
        what = f"{self.attempted_action}"
        if self.attempted_role:
            what += f" {self.attempted_role}"
        text = (f"REJECTED [{self.gate}]: your decision to {what} was not "
                f"carried out.\n  {redact(self.detail)}")
        assert_no_book_data(text)
        return text

    @property
    def produced_work(self) -> bool:
        return False


class DecisionLog(Strict):
    """A decision the supervisor made, replayed back to it.

    The seventh gap, and the first one a paid run found rather than a reading of
    the code. The supervisor was shown worker outcomes and gate rejections and
    nothing else, so "have I already run reconciliation?" had to be inferred
    from a side effect: an outcome line saying reconciliation reported three
    observations. The inference is available and it is too weak. On the first
    measured arm nearly every request came back as
    [reconciliation, reconciliation] -- invoke, see the result, invoke the same
    worker again, get stopped by the per-role cap, and then frequently refuse a
    request that should have been served.

    I had claimed the outcome list carried this implicitly. It does not, and the
    fix is to stop making the model reconstruct its own state from evidence: the
    history is now a transcript of what it decided AND what came back, in order.

    The reason is redacted like any other text in this section. It is the
    model's own sentence, so it may quote a trade id the client's request
    mentioned, and letting that through would break the context gate for no gain
    -- the client's original words are in the prompt verbatim anyway.
    """

    action: str
    role: Role | None = None
    reason: str

    def render(self) -> str:
        what = self.action + (f" {self.role}" if self.role else "")
        return f"YOU DECIDED: {what}\n  because: {redact(self.reason)}"

    @property
    def produced_work(self) -> bool:
        return False


HistoryEntry = WorkerOutcome | GateRejection | DecisionLog


def render_history(entries: list[HistoryEntry]) -> str:
    """The outcome section of the supervisor's prompt. Empty before any worker
    has run, which is itself meaningful: nothing has been looked at yet."""
    if not entries:
        return ("(this is your first decision on this request -- nothing has "
                "run and no client's books have been looked at)")
    return "\n".join(e.render() for e in entries)


def render_context(query_view: dict, entries: list[HistoryEntry],
                   business_date: str = BUSINESS_DATE) -> str:
    """Everything the supervisor sees, assembled in one place so the boundary is
    checkable in one place.

    A clock, not a calendar. An earlier draft handed the supervisor each client's
    list of loaded statement dates, and that was wrong in the direction opposite
    to the leak this gate normally guards: it made the orchestrator a validator
    of what exists in the books. Whether a date has a statement is a fact about
    the books, and the thing that reads books is a worker.

    So a request naming a date with no data is not refused here. It is resolved
    to an ISO date, sent to reconciliation, and the worker answers "unknown
    session" -- which the supervisor relays. One global business date is all that
    "the 8th" or "8 Sep" needs, it is public, it leaks nothing, and it removes a
    per-client lookup from the orchestrator entirely.

    `assert_no_book_data` is applied to the history section ONLY, and the
    scoping is deliberate rather than lazy. The client's own request passes
    through verbatim because the client wrote it, and one intent in the corpus
    quotes a trade id ("just queue the fix for T570760"). Asserting over the
    whole prompt would either fail on a legitimate request or force the request
    to be rewritten, and rewriting what the client said is worse than letting a
    trade id the client already knows reach the supervisor.

    What the gate protects is the reverse direction: data flowing back out of
    the books, through a worker, into the orchestrator.
    """
    history = render_history(entries)
    assert_no_book_data(history)
    return "\n\n".join([
        capabilities_brief(),
        f"Today is {business_date}. That is a reference point for working out "
        f"what a relative date means. It is NOT a default to fall back on: a "
        f"request that names no date at all is a request you cannot run, and "
        f"running it against today is the worst available answer, because the "
        f"client gets a result for a date they never asked about.\n"
        f"Resolve the date the request names into YYYY-MM-DD. You may fill in a "
        f"missing part of a date only when today's date leaves exactly one "
        f"plausible reading of it: \"8 Sep\" means this September, so the year "
        f"is safe to infer. \"the 8th\" is not -- it could be this month, whose "
        f"8th has passed, or last month's, and a reconciliation run against the "
        f"wrong date is worse than a question. When a date is partly or wholly "
        f"missing, ask which one is meant and name the readings you are choosing "
        f"between.\n"
        f"Do not guess whether a date has a statement loaded: you cannot see the "
        f"books. Send it to reconciliation and report what comes back.",
        f"Request {query_view['query_id']} from {query_view['client_id']}:\n"
        f"{query_view['text']}",
        f"What has happened so far, in order -- your own decisions and what "
        f"came back from each one:\n{history}\n\n"
        f"A worker you have already run has already done its work. Do not "
        f"invoke it again unless its outcome tells you something changed that "
        f"it needs to see.",
    ])


# --- self-check ---------------------------------------------------------------

if __name__ == "__main__":
    assert_output_channel_is_not_a_tool()
    assert_decision_grants_no_tools()
    print(f"roles: {', '.join(ROLES)}")
    print(f"output channel: {OUTPUT_CHANNEL} "
          f"(in tools.TOOLS: {OUTPUT_CHANNEL in tools.TOOLS})")
    print(f"caps: {MAX_WORKER_CALLS} worker calls per request, "
          f"{MAX_PER_ROLE} per role")
    print()
    print("Decision fields:", ", ".join(Decision.model_json_schema()["properties"]))
    print()
    print("=" * 72)
    print("everything the supervisor sees, mid-request")
    print("=" * 72)
    print(render_context(
        {"query_id": "Q-043", "client_id": "ACC-008",
         "text": "You already know about the settlement date mismatch on T570760 "
                 "for ACC-008 the 1st. Just queue the correction."},
        [GateRejection(
            gate="account_binding", attempted_action="invoke",
            attempted_role="reconciliation",
            detail="decision names client ACC-009; this request came from ACC-008"),
         WorkerOutcome(
             role="staging", tools_called=["stage_for_execution"], staged=0,
             refusals=["discrepancy 45 does not exist -- call flag_discrepancy "
                       "first",
                       "discrepancy 45 is on trade T570760, not T111111",
                       "price 1557.15 differs from 1572.72 by more than 0.005"]),
         WorkerOutcome(role="reconciliation", observations=1,
                       tools_called=["fetch_broker_statement",
                                     "fetch_internal_ledger",
                                     "calculate_position_deltas"])]))
