# mcp-agent-ops

A multi-agent reconciliation system behind a self-authored MCP server, and the
harness that measures whether it works.

Three heterogeneous agents reconcile a broker statement against an internal
ledger across 15 client accounts, flag discrepancies, draft client messages,
and queue corrections for a human. Six typed tools, four structural gates, 60
sessions with ground truth seeded deliberately so every number here is
recomputable from committed files.

The agent is the easy part. What this repo is actually about is the measuring:
161 tests, a scorer calibrated against eight deliberately broken agents, and a
CI gate tested against a deliberately broken repo. Three times the thing that
turned out to be wrong was the instrument rather than the system.

---

## What the measurements showed

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

# tests
for f in tests/test_*.py; do python "$f"; done
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

# variance floor
PYTHONPATH=src python src/eval.py --backend "azure:...@t0.0" \
    --variance reports/azure.jsonl reports/azure_repeat.jsonl
```

### Layout

```
src/
  generate_data.py  synthetic corpus with seeded truth
  store.py          SQLite schema — three of the four gates live in the DDL
  tools.py          the six tools, Pydantic schemas, role subsets
  server.py         MCP server, one process per (account, role)
  backends.py       the scripted stub (8 policies) and the Azure backend
  agent.py          supervisor dispatch and the worker loop
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
