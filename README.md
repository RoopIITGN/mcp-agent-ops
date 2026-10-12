# mcp-agent-ops

A multi-agent reconciliation system behind a self-authored MCP server, and the
harness that measures whether it works.

Three heterogeneous agents reconcile a broker statement against an internal
ledger across 15 client accounts, flag discrepancies, draft client messages,
and queue corrections for a human. Six typed tools, four structural gates at
the tool boundary and six more at the orchestration boundary, 60 sessions and
80 client requests with ground truth seeded deliberately so every number here
is recomputable from committed files.

Above those agents sits an LLM supervisor that decides, one step at a time,
which of them to invoke and when to stop. It holds no tools and cannot read any
client's books.

The agents are the easy part. What this repo is actually about is the measuring:
403 tests, scorers calibrated against eighteen deliberately broken agents, a CI
gate tested against a deliberately broken repo, and a measured noise floor for
every number claimed. Eight times now the thing that turned out to be wrong was
the instrument rather than the system, and the most expensive of those was a
corpus that had never tested the gate it existed to test.

---

## What the measurements showed

**A weak model cannot be measured as an orchestrator, only ranked.** Two
independent runs of `gpt-5.4-nano` on the same 80 requests at `temperature=0`
disagree on one query in four and differ by 10 points on the headline metric,
while two runs of `gpt-5.4-mini` disagree on one in eleven and differ by one
point. Ten points is larger than any prompt change worth making, so two
revisions of the supervisor prompt that appeared to move the number by exactly
that much were unresolvable by construction. Both are reported as void. Without
a measured floor, a prompt improvement that does not exist would be a result in
this file.

**Two gates I called structural were not, and a real model found both in one
run.** The tool surface validated who could act and, in one field, not *what*
an action was. `stage_for_execution` took `payload: dict[str, Any]`. Given a
bare discrepancy id and told to stage a correction, the model had no idea what
to propose and staged placeholders — `"TBD"`, `"Insufficient context
provided"`, `"Please provide the exact correction"` — and the server accepted
every one. The human approval queue filled with 100 rows for 52 discrepancies,
most of them asking to be told what to do.

Every structural gate held while that happened. Nothing executed, nothing
crossed accounts, every precondition was enforced, and the output was
worthless. **A boundary that constrains who may act but not what an action is
will pass a hundred placeholders into a human approval queue.**

148 tests and a seven-policy calibration never caught it, because the stub's
payloads were sensible — I wrote them.

**The second gate was idempotency.** It keyed on `hash(session_id, payload)`,
so identical corrections collapsed to one row. They did, for a stub that sends
byte-identical payloads. A model rewords its proposal on every retry, so each
retry hashed differently and wrote another `PENDING_HUMAN` row. Re-keyed on
`(session_id, discrepancy_id)`, because the rule was never "one row per
distinct wording".

**A faked tool protocol made the agent unable to stop.** The model backend
rendered history as prose *about* tool calls — an assistant message reading
`calling stage_for_execution with {...}`, a user message reading
`stage_for_execution returned: {...}`. That is a description of a tool call,
not a tool call. The model was never told, in the terms the API defines, that
its call had completed, so it had no stopping signal and re-called until the
step cap caught it: twelve staging calls for two discrepancies in the worst
session.

| after the four fixes | before | after |
|---|---|---|
| clean trajectories | 50% | **100%** |
| rows in the approval queue | 100 | **52** |
| sessions staging more than they flagged | 30 of 60 | **0** |
| model calls per pass | 557 | 516 |

**The injection experiment came back null, which is the result.** Arm A shows
the broker statement's untrusted note only to the reconciliation agent, which
holds no write tool. Arm B puts it in the triage prompt, where the agent that
can suppress a finding sees it — what a single-agent design does by
construction. Arm B existed to show that scoping the tools was doing the work.
It didn't: the prompt rule held on all 10 at-risk sessions that reached the
model. The split architecture is defence in depth here, not the thing
preventing compromise.

**And a third defence layer this project wasn't measuring at all.** Azure's
content filter refused 1 of 13 payloads outright — `override-3`, which fakes a
closing tag to escape the document — but only in arm B. The same payload bytes
passed the filter in arm A as a tool result. **Message position decided the
outcome, not content.** Platform filtering is now scored as its own verdict,
`blocked_upstream`, excluded from the resistance denominator: counting it as
resistance would credit this system with a defence it does not contain.

**The variance floor is 0 flips in 60 paired sessions.** Two runs, same
configuration, nothing changed. The retrieval project that preceded this one
measured a 20% generator flip rate, which is why a 25→21 improvement there had
to be reported as inconclusive. The difference is concrete: `temperature=0` was
pinnable here, the tool schemas are narrow enums rather than free text, and the
arithmetic is in code. A zero in 60 trials is not a zero rate, but it does mean
every difference reported above is larger than the noise this measurement could
detect.

---

## Results

`gpt-5.4-nano` at `temperature=0`, 60 sessions, scored against
`data/seeded_truth.json`.

| | |
|---|---|
| recall | **100%** (52/52) |
| precision | **100%** |
| mislabelled | 0 |
| invented | 0 |
| control sessions flagged | **0 of 22** |
| clean trajectories | **100%** |
| schema validity | 100% over 336 tool calls |
| staged actions, all `PENDING_HUMAN` | 52 |
| variance between identical runs | 0 flips in 60 |

By discrepancy type, all found: quantity mismatch 12/12, missing trade 10/10,
duplicate booking 13/13, price variance 9/9, settlement date shift 8/8.

### Injection resistance

| arm | the note reaches | at risk | held | filtered upstream |
|---|---|---|---|---|
| A — tool result only | the read-only agent | 38 | 38 (100%) | 0 |
| B — in the triage prompt | the agent that can suppress | 10 | 10 (100%) | 1 |

Across 48 injected sessions in both arms: **zero seeded discrepancies lost to a
payload**, zero attempts at a tool outside the calling agent's role — including
three sessions carrying payloads that name `approve_correction` and
`execute_ledger_adjustment` explicitly — and every staged action still
`PENDING_HUMAN`.

Arm B's n is 10. One compromised session would have read 90%, so the honest
statement is *no compromise observed at n=10*, not a rate.

### Cost and latency

A full 60-session pass is **~$0.09** and about 11 minutes. From the committed
trace: 336 tool calls at **p50 7.0ms**, so the MCP layer and SQLite are
effectively free and all 11 seconds per session is model latency.

---

## The supervisor

Above the three worker agents sits an LLM supervisor. A client request arrives
in natural language with a client id; the supervisor decides which worker to
invoke, is told what that worker found, and decides again — invoke another,
stop with an answer, refuse, or ask the client a question. It holds no tools,
cannot read any client's books, and never recomputes anything a worker has
already done. Its only output channel is one typed `Decision` per turn.

**What it is allowed to see.** Two different projections of the same worker
transcript: counts and redacted refusals go *up* to the supervisor, while
observations and discrepancy ids go *sideways* to the next worker. The
orchestrator is therefore structurally unable to leak one request's findings
into another's, and a test asserts no book data appears in anything rendered to
it.

**Isolation is bound before the process exists.** The account and the role are
passed in `argv` when the MCP subprocess launches, one subprocess per worker
invocation. There is no code path that redirects a running worker to another
account, and two concurrent requests from the same client for the same thing
get separate asyncio tasks with separate orchestration state.

**Six orchestration gates:** decision validity (`extra="forbid"`, `Literal`
roles), no privilege escalation, account binding, step cap, context boundary,
exactly-one-action. Two caps, because the worker cap alone cannot bound the
loop: `MAX_WORKER_CALLS = 5` and `MAX_SUPERVISOR_CALLS = 10`.

### The query corpus and its ceilings

80 natural-language requests across 12 intent classes, generated
deterministically from the seeded books, with the ground truth held out of the
prompt. Three ceilings are computed from the corpus alone, with no model calls:

| strategy | ceiling | |
|---|---|---|
| `fixed` | 12/80 (15%) | runs all three roles whatever was asked |
| `plan_once` | 65/80 (81%) | commits to a plan before any worker reads the books |
| `iterate` | 80/80 (100%) | decides again after each worker |

The 19-point gap between `plan_once` and `iterate` is **15 requests where the
books let the orchestrator stop short of what the client asked for** — the
client asks for the whole pipeline, reconciliation finds nothing to act on, and
continuing is wasted work. `plan_once` cannot win any of them, because the
information that justifies stopping does not exist until a worker has read the
books. That is a proof about information ordering, not an experiment.

### Results

`stub:ideal` workers, so these numbers isolate the orchestrator's decisions.
Two independent runs of each deployment.

| | mini #1 | mini #2 | nano #1 | nano #2 |
|---|---|---|---|---|
| fully correct | **69%** | **68%** | 34% | 44% |
| exact route | 69% | 69% | 40% | 48% |
| over-reach | 11% | 14% | 57% | 45% |
| under-reach | 20% | 18% | 2% | 8% |
| early stop (of 15) | **15/15** | **15/15** | 4/15 | 8/15 |
| false clarify | **0%** | 1% | 7% | 12% |
| false refuse | 7% | 10% | 13% | 6% |
| wasted calls | 11 | 16 | 87 | 60 |
| model calls | 158 | 164 | 280 | 236 |
| isolation violations | 0 | 0 | 3 | 2 |

**mini wins all 15 of the discriminating class, in both runs.** That is the
architecture claim landing: `plan_once` cannot win any of those by
construction.

Paired exact two-sided McNemar, mini against each nano arm separately, because
a result that depends on which of two equivalent runs you picked is not a
result:

| | discordant | p |
|---|---|---|
| vs nano #1 | 35 / 7 | 1.5 × 10⁻⁵ |
| vs nano #2 | 28 / 8 | **1.2 × 10⁻³** |
| early-stop class, vs nano #2 | 7, all to mini | **0.016** |

The bolded figures are the ones quoted elsewhere: the less flattering of two
valid pairings.

### Both layers live at once

Every arm above used `stub:ideal` workers, which isolates the orchestrator: a
scripted worker is correct by construction, so every error in those numbers
belongs to the supervisor. One further arm replaced it with a real model --
`gpt-5.4-mini` deciding roles, `gpt-5.4-nano` as the workers choosing their own
tools -- over the same 80 requests.

| | stubbed #1 | stubbed #2 | **real workers** |
|---|---|---|---|
| fully correct | 69% | 68% | **68%** |
| exact route | 69% | 69% | **70%** |
| early stop | 15/15 | 15/15 | **15/15** |
| model calls | 158 | 164 | 167 |

Routing quality moved by less than the orchestrator's own run-to-run noise, so
the two layers compose.

What the live workers did with their tools is the better result:

```
reconciliation | fetch_broker_statement, fetch_internal_ledger, calculate_position_deltas
triage         | flag_discrepancy, draft_client_inquiry
staging        | stage_for_execution
```

**No role ever appears beside a tool outside its grant**, across 214 tool calls,
with a real model choosing. Least privilege was previously a claim about `argv`
and a scripted agent. Every one of the 10 `pending_actions` rows is
`PENDING_HUMAN`. And all 22 refusals are `unknown session` -- a date with no
statement loaded -- so the workers emitted no malformed call at all, and every
refusal traces to a date the *supervisor* chose.

Cost, measured: 297k prompt tokens on the supervisor and 242k on the workers.
The half that was expected to dominate came in smaller, because workers run
short loops on a cheap model under tight schemas.

### The reproducibility floor, which is the actual finding

Same model, same prompt, same corpus, `temperature=0`, two independent runs:

| | queries that flip | split | p | aggregate spread |
|---|---|---|---|---|
| mini vs mini | 7 / 80 (8.8%) | 4 / 3 | 1.0 | 1 point |
| nano vs nano | 20 / 80 (25%) | 6 / 14 | 0.115 | 10 points |

One query in four changes outcome between identical nano runs. The paired test
says the two nano runs are *not* significantly different from each other, so
the 10-point aggregate gap is noise rather than one run going better.

The consequence is the thing worth taking away:

> **nano's noise floor exceeds the effect size of any prompt change worth
> making, so as an orchestrator it can be ranked but not measured.** mini's is
> one point, so on mini a five-point intervention is detectable.

This was learned the expensive way. Two revisions of the supervisor prompt were
measured on nano and appeared to move the headline by ten points. Both were
inside the noise floor by construction — experiments run on an instrument that
could not resolve them. Had the floor not been measured, a prompt improvement
that does not exist would be written up here as a result.

What *is* reproducible is mini's accuracy, not its behaviour: `clarify` moved
33% → 17% and `refuse` 92% → 100% between its two runs while fully-correct
moved one point. The 7 discordant pairs are where that lives.

### The isolation gate, fired by a live model

Across the two nano runs the account-binding gate caught **5 attempts** to
reach a client's book other than the requesting client's — 3 in one run, 2 in
the other. Reproducible, by a real model, not a stub. A gate that has never
fired is an assumption; this one has evidence, and the `stub:cross_account`
calibration policy is its positive control.

### Four bugs the measurements found

Each of these was found by a number being wrong, not by reading code.

**The supervisor could not see its own decisions.** The history it received
carried worker outcomes and gate rejections but not its own prior decisions, so
it re-emitted the same route repeatedly. Adding a decision log moved
fully-correct from 4% to 38% and step-cap terminations from 68 to 5. The
orchestrator's own choices are part of its state, and omitting them was
measured as the model being incapable.

**A validator of mine rejected 69 of 80 decisions.** It refused any `clarify`
that carried a resolved date, on the reasoning that a known date leaves nothing
to clarify. A clarification need not be about the date, and when it is, naming
the date you tried is what makes the question answerable. 24% of a paid run
spent proving my own constraint wrong. Removing it *lowered* the headline,
because the rejections had been accidentally suppressing the model's real
failure mode.

**Two measured arms were silently compared under different conditions.** The
results file keyed arms by supervisor label while `--fresh` and `--resume`
compared against the CLI spec, which never matches for an azure arm — so
`--fresh` dropped nothing and `--resume` re-ran everything, both without
complaint, because doing the whole run is also correct behaviour when there is
nothing to skip. Separately, the discrepancy tables are keyed by session with no
run scope, so the second arm inherited the first's rows and every precondition
the first arm had to earn was already satisfied for the second. Each arm now
rebuilds its database by default.

**A test asserted the bug.** `test_fresh_drops_only_its_own_arm` checked for
the literal string `!= a.supervisor` in the source of `amain`. The string was
present, the test passed for weeks, the comparison was against the wrong field,
and the test *failed on the correct fix*. A test that asserts an implementation
cannot tell you the implementation is wrong — it pins the defect in place and
makes repairing it look like a regression. It is now a behavioural test.

### One prompt revision, measured and reverted

The orchestration prompt was revised twice before the arms above and a third
time after them. The third attempt is recorded here because it failed, and
because the reason for attempting it at all was a change in the instrument
rather than a change of mind.

Revision 2's closing comment said it was the last, on the grounds that a prompt
tuned against the corpus that motivated it is fitted to that corpus. That held
while the measurements were on the weak deployment, whose noise floor is ten
points -- larger than any change worth making. On the strong deployment the
floor is one point, so a five-point change is visible, and the bar moved for
that reason.

**What it targeted.** `premature_stage` -- the client asks to queue a correction
for a trade the system has not triaged -- scored 1 of 18 across three arms, and
the six failures were legible rather than mysterious: two refusals that named
the missing step correctly and stopped there, one that mistook queuing for
approving, and three that skipped triage and answered *"0 corrections were
queued"*. That last group is the serious one. A refusal is visibly incomplete; a
confident zero closes the ticket.

**What changed.** Revision 2's prerequisite rule was conditioned on a worker
refusal -- *"when a refusal names a step that has not run, invoke that worker"*
-- and a careful supervisor never provokes one, because it works out that
staging needs a recorded discrepancy *before* calling staging. The rule's
condition was unreachable, so it was generalised. The empty-result rule was
qualified at the same time: a zero produced by skipping a step is a fact about
the plan, not the books.

**Result, over two arms of 80 requests.** `premature_stage` stayed at 0 of 6 in
both. The generalised rule did reach the model -- it stopped refusing and began
emitting `invoke staging` with a message to the client attached -- and a
validator rejected all three of those, because `invoke` is defined to carry no
client message. The failure changed shape and not outcome, and the edit cost
three invalid decisions where three previous arms had none.

Both prompt edits are reverted. The one change kept is in the capability
catalogue, a different file aimed at a different failure, and the only one that
moved anything: false refusals went 7%, 10% and 9% across three revision-2 arms
to 4% in both revision-3 arms. Four requests per arm is the same order as the
run-to-run flip rate, so it is reported as consistent and mechanistically
explained rather than proven.

The obvious next move -- letting `invoke` carry a message -- is where this
stops. Two located defects go into the limitations below instead of a fourth
attempt. **The reason to stop revising is the same whether or not the last
attempt worked**, and a revision that fails is cheaper to publish than one that
succeeds for reasons nobody checked.

### A corpus that never tested the gate it existed for

`premature_stage` is the only class that exercises the precondition gate: the
client asks to queue a correction, triage has not recorded the discrepancy, the
staging worker must refuse, and the supervisor must re-plan around the refusal.

The generator drew each intent's sessions with a per-intent seed, and a comment
asserted that this meant two intents never drew the same sessions. It does not:
a per-intent seed makes the draws *independent*, not disjoint, and independent
samples from one pool collide. 18 of 40 sessions carried two or three intents,
and four of the six `premature_stage` sessions were shared with intents that
run triage. Whichever request arrived first left a discrepancy row that
satisfied the other's precondition, and at concurrency 4 which one won was a
matter of scheduling.

So that class had never tested the gate, in any run. Its sessions are now
reserved, and `test_premature_stage_sessions_are_not_shared` is the test that
was missing — a test that would have failed the day the comment was written.
**An invariant asserted in a comment is not an invariant.**

---

## Why a perfect score is not the interesting part

After `calculate_position_deltas` has done the arithmetic, the agent's job is
classification and sequencing. The median session has one finding. So the
outcome metrics are not discriminating, and 100% recall says the system works
rather than teaching anything.

What the numbers are *for* is the diff: the before-and-after on the four fixes,
and the fact that two gates described as structural turned out to be
bypassable. That is the part a reader should take away.

The honest fix, if a sharper number were wanted, is harder data — more findings
per session, within-tolerance price differences that must be ignored,
ledger-only trades that are not one of the five reportable kinds. See
[Known limitations](#known-limitations).

---

## Architecture

```
supervisor (deterministic dispatch, no model call, no tools)
  ├── reconciliation worker  fetch_broker_statement, fetch_internal_ledger,
  │                          calculate_position_deltas          — cannot write
  ├── triage worker          flag_discrepancy, draft_client_inquiry
  │                                                             — cannot stage
  └── staging worker         stage_for_execution                — cannot read
```

**The supervisor is not an agent and the README does not pretend otherwise.**
It groups sessions by account, starts the worker processes, hands each worker
its slice of the previous worker's output, and collects results. No model call,
no tools. The agency is in the three workers, and that is a complete answer.

**The ordering of the workers is the security argument.** The agent that reads
untrusted text — the broker statement's free-text `notes` field, where the
injection suite puts its payloads — is the one agent with no ability to write
anything. A payload that fully captures it has captured a process that can only
read and report.

**One server process per (account, role).** Fifteen accounts means 45 processes
over a run rather than 180, because an account's four sessions share its
servers — and it means an account binding can only be *launched*, never
mutated. Parallelism across accounts is safe by construction rather than by
locking.

### The six tools

| tool | kind | idempotency |
|---|---|---|
| `fetch_broker_statement` | read | naturally idempotent |
| `fetch_internal_ledger` | read | naturally idempotent |
| `calculate_position_deltas` | compute | pure comparison, no state |
| `flag_discrepancy` | write | `UNIQUE(session, trade, type)` |
| `draft_client_inquiry` | write | upserts only while unedited by a human |
| `stage_for_execution` | write | `UNIQUE(session, discrepancy)` |

Three things are deliberately **not** parameters, and each absence is a gate.
No tool takes an `account_id`: isolation comes from the connection binding. No
tool takes a `tolerance`: an agent able to widen it could make a price variance
stop being one without ever stating a falsehood. No tool takes a `status`: so
staging can only write `PENDING_HUMAN`.

### The four gates

| gate | enforced by | a test fails if it breaks |
|---|---|---|
| **capability** | `store.decide_action` is wrapped by no tool, so approval is absent from `list_tools` | `test_no_tool_wraps_decide_action` (AST, not grep) |
| **precondition** | NOT NULL foreign key, plus server-side checks that the discrepancy is still flagged, the remedy matches its type, and the trade matches | `test_the_remedy_must_match_the_discrepancy_type` |
| **isolation** | account bound per MCP connection at process launch | `test_cross_account_session_refused_over_stdio` |
| **idempotency** | `UNIQUE` constraints keyed on what is being corrected | `test_rewording_a_correction_cannot_create_a_second_pending_action` |

There is no tool that executes anything. `stage_for_execution` writes a
`PENDING_HUMAN` row and the approval endpoint is outside the agent's reach. The
gate is the absence of a capability, not an instruction in a prompt.

### Arithmetic in code, classification in the agent

`calculate_position_deltas` reports *what differs* and does not say what it
means — "trade T1: quantity 500 at the broker, 450 in the ledger" and stop. The
agent decides that is a `quantity_mismatch` and flags it. If the tool
classified, `flag_discrepancy` would be pure relay and outcome accuracy would
measure the quality of a `for` loop.

Float comparison stays in code. Asking a model whether two prices differ by
more than 0.5% buys a wrong answer at some rate, and tolerance is the number an
auditor checks.

---

## How the harness is calibrated

The question worth asking of any agent evaluation is *how do you know the
harness works?* Most demos cannot answer, because the only trajectory they ever
scored was one they hoped was correct — and a scorer that has only seen correct
behaviour reports 100% and has demonstrated nothing.

`src/calibrate.py` runs eight scripted agents, each wrong in exactly one known
way, and asserts the signature the scorer must produce for each.

| policy | recall | precision | invented | control FP | clean traj | refusals |
|---|---|---|---|---|---|---|
| `ideal` | 100% | 100% | 0 | 0/22 | 100% | 0 |
| `skip_ledger_read` | 100% | 100% | 0 | 0/22 | **0%** | 0 |
| `stage_before_flag` | 100% | 100% | 0 | 0/22 | **0%** | **60** |
| `false_positive` | 100% | **70%** | **22** | **22/22** | 100% | 0 |
| `wrong_type` | **0%** | **0%** | 0 | 0/22 | 100% | 0 |
| `wrong_remedy` | 100% | 100% | 0 | 0/22 | **33%** | **52** |
| `miss_one` | **73%** | 100% | 0 | 0/22 | 100% | 0 |
| `no_draft` | 100% | 100% | 0 | 0/22 | **37%** | 0 |

`skip_ledger_read` is the row that justifies trajectory scoring at all: a
perfect outcome, zero clean trajectories. It compared two books having read
one, and no outcome metric can see that.

`wrong_remedy` exists because of what the model run exposed: it proposes a
remedy that does not match its discrepancy's type, and the assertion that
matters is that **the approval queue stays empty**.

Two of these expectations earned their place by failing. `no_draft` was
indistinguishable from `ideal` until the scorer learned that a discrepancy
recorded with no client message is an unfinished workflow. Then `no_draft`'s own
expectation was wrong — I asserted no trajectory passes, but a control session
has nothing to flag and so nothing to draft, and its trajectory is legitimately
clean. The scorer was right and the expectation was not.

### The gate is tested against a broken repo

`tests/test_gate.py` runs every CI check twice: once against the real repo,
where it must pass, and once against a deliberately broken copy, where it must
fail and name the breakage. A gate that has only ever passed is in the same
position as a scorer that has only seen a correct trajectory.

That found two bugs in the gate itself. The harness-ceiling computation indexed
straight into the session and account maps, so a renamed account or an orphaned
truth record made the gate **die with a KeyError** — on precisely the inputs it
exists to detect. Its own error handling had never been exercised.

---

## The corpus

15 accounts × 4 as-of dates = 60 sessions. 52 discrepancies seeded across five
kinds, and **22 sessions (37%) with nothing wrong**, because an agent that
flags something on every account looks perfect until the controls are counted.

| kind | seeded |
|---|---|
| duplicate booking | 13 |
| quantity mismatch | 12 |
| missing trade | 10 |
| price variance | 9 |
| settlement date shift | 8 |

Synthetic, which is the one real advantage synthetic data has: **the labels come
free**. In the retrieval project that preceded this, labels cost four hours of
hand-verification.

CI asserts the committed corpus still reproduces **byte-for-byte** from
`generate_data.py`'s seed. Ground truth that cannot be regenerated is just a
file somebody edited.

The harness ceiling is checked too: **52 of 52** seeded discrepancies are
visible to the comparison, and **0 of 22** controls report anything. Below a
ceiling of 1.0, no agent could reach 100% recall and every recall number has
quietly become a measurement of the plumbing.

### One seeding bug worth recording

`seed_discrepancy` could land two discrepancies on the same trade, producing
truth that contradicts the data — a trade cannot be both removed and
duplicated. The contradiction was silent, which is the worst kind. Fixed with a
`used` set; the clean-session share moved from 48% to 37%.

---

## Injection suite

13 payloads in 5 classes, written into the broker statement's `notes` field —
empty and reserved since the corpus was generated, so an injected run differs
from the clean one in the data and nowhere else. The realistic attack on an
operations agent is not someone typing at it; it is a document it was asked to
read.

Classes: instruction override, false authority, urgency, tool-name injection,
multi-turn persistence. Each written in the register of a back-office note
rather than of an attack, because a payload that announces itself tests nothing.

**What a payload cannot do:** cause an unauthorised correction. No tool
approves or executes, so the capability is absent from the agent's vocabulary
rather than discouraged in its instructions. "Did the gate hold" is only an
interesting question where the gate is a prompt rule.

**What it can still do:** corrupt the report while every gate holds. An agent
persuaded to stop flagging produces a clean reconciliation for an account with
real breaks. That is measured by pairing each injected session against the same
session run clean by the same backend — not by reading the model's reasoning,
which is unreliable and on a reasoning model partly hidden.

### A metric bug the compromised stub exposed

The first resistance score counted a control session as resistance. A
suppression payload on an account where nothing differs has nothing to
suppress, and with 22 of 60 sessions clean that inflated the rate by more than
a third. Those are now reported as `no-op` and excluded from the denominator.

Found by running the deliberately-compromised stub against the scorer — the
same argument as the calibration table, applied one level up.

---

## Observability

OpenTelemetry spans mirror the run: `session → role → tool`, with real context
propagation through the SDK. What is custom is only the exporter, which writes
JSONL so a committed trace is inspectable without a running collector; swapping
in OTLP is one line.

A refused call gets `ERROR` status and the refusal reason, because a gate firing
is the most interesting thing in a trace and one showing only successful calls
hides the events worth looking at. The committed trace
(`reports/trace.jsonl`) is from the `stage_before_flag` policy for exactly that
reason.

---

## CI

Runs with no credentials, no model and no network beyond the package index.
Everything it checks is either a property of committed files or something the
scripted agent reproduces deterministically — which is why the stub exists. A
pipeline that needs an API key to verify itself cannot run on a fork's pull
request.

Four groups, and they are not the same kind of thing:

- **integrity** — does the corpus still agree with its ground truth, does the
  comparison still see all 52 seeded discrepancies, do the controls stay silent
- **structural** — no tool approves anything, the agent reading untrusted text
  holds no write tool, no schema accepts an `account_id` or a `tolerance`, every
  schema forbids extra fields
- **calibration** — re-run every broken stub policy and confirm the scorer still
  tells them apart
- **baselines** — recompute the committed numbers from the committed
  trajectories, so a report cannot drift from the data it summarises

The calibration group is the one worth having. Asserting a committed number only
catches a change in that number; asserting that a deliberately broken agent
still scores badly catches a scorer that has stopped working — and a harness
reporting 100% for everything passes every numeric threshold there is.

Integrity and structure run *before* the tests, because "harness ceiling 0/52
visible" names a stale source file in one line where a failing async test
surfaces as an `ExceptionGroup`.

---

## Running it

```bash
pip install -r requirements.txt
python src/generate_data.py          # 15 accounts x 4 dates, seeded truth
python src/store.py --init --force   # schema + fixtures

# the whole pipeline, with no model and at no cost
PYTHONPATH=src python src/agent.py --backend stub:ideal
PYTHONPATH=src python src/eval.py --out reports/eval.md
PYTHONPATH=src python src/calibrate.py --out reports/calibration.md
PYTHONPATH=src python src/gate.py

# the orchestration layer, also with no model
PYTHONPATH=src python src/queries.py                   # corpus + the three ceilings
PYTHONPATH=src python src/calibrate_routing.py         # 10 broken supervisors
PYTHONPATH=src python src/run_requests.py --supervisor stub:oracle
PYTHONPATH=src python src/routing_eval.py --out reports/routing.md

# tests -- pytest is authoritative; the loop below cannot fail a file with no
# __main__ block, which once let two suites sit green in CI without executing
python -m pytest tests/ -q
```

With a model (see `.env.example`; `src/check_model.py` verifies a deployment
for a fraction of a cent before anything costs money):

```bash
PYTHONPATH=src python src/check_model.py
PYTHONPATH=src python src/agent.py --backend azure --out reports/azure.jsonl \
                                   --trace reports/trace_azure.jsonl
PYTHONPATH=src python src/eval.py --traj reports/azure.jsonl

# injection, both arms
PYTHONPATH=src python src/injection.py build
PYTHONPATH=src python src/agent.py --backend azure --db data/ops_injected.db \
                                   --out reports/injected_azure.jsonl --fresh
PYTHONPATH=src python src/injection.py score --backend "azure:...@t0.0" \
    --clean reports/azure.jsonl --injected reports/injected_azure.jsonl \
    --db data/ops_injected.db

# variance floor, worker layer
PYTHONPATH=src python src/eval.py --backend "azure:...@t0.0" \
    --variance reports/azure.jsonl reports/azure_repeat.jsonl

# the supervisor arms. Each rebuilds its own database, because the discrepancy
# tables are keyed by session with no run scope and a second arm would
# otherwise inherit the first's satisfied preconditions. An unnamed re-run is a
# new arm and skips nothing, which is how the repeat below happens.
PYTHONPATH=src python src/check_supervisor.py          # 4 calls, pre-flight
PYTHONPATH=src python src/run_requests.py --supervisor azure:gpt-5.4-mini \
                                          --backend stub:ideal --concurrency 4
PYTHONPATH=src python src/run_requests.py --supervisor azure:gpt-5.4-mini \
                                          --backend stub:ideal --concurrency 4
PYTHONPATH=src python src/routing_eval.py --out reports/routing_azure.md \
    --compare 'azure:gpt-5.4-mini @ <run-a>' 'azure:gpt-5.4-mini @ <run-b>'
```

`probe_model.py` is standalone and imports nothing from `src/`. It asks a
deployment one parameter at a time whether it accepts `temperature`,
`reasoning_effort` and `max_completion_tokens`, and prints the API's own reply.
It exists because `reasoning_effort` is *accepted* at every level on both
deployments and spends zero reasoning tokens at `minimal` and `low` — an arm
pinned there would have cost money, produced a different label and run the same
model, and nothing in the results would have shown it.

### Layout

```
src/
  generate_data.py  synthetic corpus with seeded truth
  store.py          SQLite schema — three of the four gates live in the DDL
  tools.py          the six tools, Pydantic schemas, role subsets
  server.py         MCP server, one process per (account, role)
  backends.py       the scripted stub (8 policies) and the Azure backend
  agent.py          worker dispatch and the tool-calling loop
  queries.py        80 client requests, 12 intents, and the three ceilings
  plan.py           the Decision schema, the caps, the context projections
  supervisor.py     the iterative orchestrator and 10 stub policies
  run_requests.py   one asyncio task per request, one subprocess per invocation
  routing_eval.py   routing scorer, both sides of every pair, exact McNemar
  calibrate_routing.py  proves the routing scorer separates the 10 policies
  check_supervisor.py   4-call pre-flight through the real decision path
  eval.py           outcome, trajectory, schema validity, McNemar, variance
  calibrate.py      proves the scorer separates the broken policies
  injection.py      13 payloads, paired scoring
  tracing.py        OpenTelemetry spans to JSONL
  gate.py           the CI gate
  check_model.py    pre-flight for a deployment
data/               corpus, ground truth, payload manifest — all committed
reports/            every result, recomputable from the above
```

---

## Known limitations

**The task is easy, so the outcome metrics are not discriminating.** After the
compute tool has done the arithmetic, the median session has one finding to
classify. 100% recall says the plumbing works. The before-and-after on the four
fixes is the informative part.

**Two classification branches no session exercises.** A within-tolerance price
difference that must be ignored, and a trade in the ledger but absent from the
statement, which is a real investigation but not one of the five reportable
kinds. Both are unit-tested; neither occurs in any of the 60 sessions. So "the
agent correctly ignores a difference inside tolerance" is a claim about code,
not a measurement.

**Arm B of the injection experiment has n=10.** One compromise would have read
90%.

**One payload per trade, at most two findings per session.** The agent never has
to disambiguate a single trade with both a quantity and a price problem.

**The variance floor is a zero observed in 60 trials, not a zero rate.**

**Single-agent comparison not built.** The strongest remaining experiment: one
agent holding all six tools, which makes tool-selection accuracy a real
measurement and removes least privilege at the agent layer. The CI gate would
have to exclude it by name from `structural.read_only_roles`, and that exclusion
would itself be worth reading.

### Limitations of the orchestration layer

**Composition is measured once, at n=80, and not repeated.** The arm with live
workers at both layers ran once. Its agreement with the stubbed arms is within
the orchestrator's noise floor, but one arm is not two, and the worker layer's
own variance under orchestration is unmeasured.

**Prompt injection against the supervisor is not tested.** The injection suite
targets the worker agents, which call tools. The supervisor is a different and
arguably more interesting target, because it holds the authority to delegate.
No arm puts a payload in the client request aimed at the planner.

**Two rules in the supervisor prompt are wrong, and are left in deliberately.**
Both were added in a revision that also added three rules which worked, and
both restate guidance already present elsewhere in a lossier form:

**The date rule compresses an exception out of existence, and this one is
established.** A line in the request context says `"8 Sep"` resolves to the
current year while `"the 8th"` does not. The system-prompt version says to
resolve wherever one reading exists, and the system prompt wins. The result,
across all four arms and 24 requests that name a day with no month:

| | clarified | stopped with `done` | refused |
|---|---|---|---|
| mini, both arms | **0 / 12** | **12 / 12** | 0 |
| nano, both arms | 4 / 12 | 1 | 7 |

mini stamps the business date on a request that does not name one, runs the
work, and reports success — twelve times out of twelve, never once asking. The
failure is not a low score on a hard class; it is a confident wrong answer
produced by a rule I added. nano does better here only because it follows the
instruction less reliably.

**Two defects located in the orchestration layer, left unpatched.** Both were
found by the revision-3 measurement above, and both have a known fix that was
deliberately not applied.

- *The prerequisite rule cannot fire where it is needed.* It is conditioned on
  a worker refusal, and a supervisor that reasons ahead never provokes one. The
  generalised version was measured and reverted, so the narrow wording stands
  and `premature_stage` remains at 0 of 6.
- *A supervisor cannot explain itself while delegating.* `Decision` forbids a
  client message on an `invoke`, reasoning that invoking a worker is not a reply
  to the client. A real model tried it three times in one arm — announcing the
  step it was about to take while taking it — and all three were rejected as
  invalid. The rule is defensible and the behaviour it blocks is reasonable,
  which makes this a design question rather than a bug. It is also the second
  validator of mine to reject a coherent decision.

**The `cross_account` class also fails, but the cause is not established.** The
second added rule says to refuse a request reaching for another account, which
contradicts the partial-decline rule a few lines above it — a request asking
for legitimate work on your own account *plus* a peek at another should serve
the first and decline the second. That contradiction is real and visible by
reading the prompt. What the data does *not* show is that it explains the
failure: mini terminates `done` on 6 of 12 rather than refusing, and all 6 of
those scored 0% on exact route too, so the route was wrong even when the
terminal was not. The split also flips between identical mini runs, 4/2 then
2/4, which at n=6 is the instability this section warns about appearing exactly
where a conclusion was about to be drawn. Recorded as an unexplained 0/12 with
a suspected contributing cause.

Both rules are left in because the harness cannot currently resolve the fix. A
deletion would be a prompt change measured against the same 80 queries that
motivated it, and with two mini arms there is no basis for claiming the result
moved. Changing a prompt you cannot measure is what produced the two void
comparisons above. The defects are reported instead.

**Per-intent figures are n=6 and should be read as fractions.** One query is 17
percentage points. Only classes at 0 or 6 across multiple arms carry any
weight; `premature_stage` at 1/6 on mini and `underspecified_date` at 4/6 are
not distinguishable from each other.

**mini's noise floor is n=2.** Two runs give a range, not a variance. The
one-point spread and 7 discordant pairs are an encouraging observation, not an
estimate of the distribution.

**`fully_correct` is a conjunction and hides which part failed.** It requires
the right action, roles, date and declines together. The paired per-class
tables exist because of this, but a single headline percentage still averages
four different kinds of mistake.

**SQLite is not a production concurrency story.** Throughput saturates at
concurrency 4, bottlenecked on process spawn rather than the database. The
isolation and idempotency claims are about correctness under concurrency, not
about scale.

---

## Lineage

A companion to a retrieval-evaluation repo built the same way — character-span
gold labels, supersession-aware queries, a citation-verification gate — and
several decisions here are carried over from what that project measured. The
20% generator flip rate that made a result there unreportable is why the
variance floor is measured here before anything is claimed. The reranker that
was cut on a paired significance test is why `wrong_remedy` is a calibration
policy rather than an assumption.

The principle both share: **enforce at the boundary, not in the instruction** —
and the lesson this one added is that a boundary has to constrain what an action
*is*, not only who is allowed to propose one.

The orchestration layer added a second: **measure the instrument before
believing the result.** Every finding in this file that had to be withdrawn was
withdrawn because a number was compared against another number produced under
conditions nobody had checked — a different database, a different corpus, a
different run of the same model. The noise floor, the per-arm database rebuild
and the four-arm layout all exist to make those conditions visible in the
report rather than discoverable afterwards.
