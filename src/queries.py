"""
The client request corpus: natural-language queries with routing truth.

Why this file exists is the whole argument for Phase 8, so it is worth stating
plainly.

The system used to run the same three workers in the same order for every
session. A supervisor that decides which workers to run therefore had nothing
to decide: the only correct answer to the only task in the corpus was "all
three, in order". Putting a language model in that position and calling it an
orchestrator would have produced a trace identical to the hardcoded pipeline's,
at higher cost, with no measurement to show it had helped. Decoration.

Routing becomes a real decision only when requests differ. So this file adds a
layer above the trade data -- who is asking, and for what -- and the trade and
ledger corpus underneath is untouched.

Two things the corpus is built to discriminate
----------------------------------------------
There are three ways to decide which workers run, and this corpus separates
them by construction. Each ceiling below is COMPUTED from the corpus, with no
model calls at all, which is what makes the comparison honest.

  fixed       run all three workers, every time, whatever was asked.
              Wrong on every request that wanted less.

  plan_once   read the request, commit to a full route, execute it.
              Gets intent right. Cannot get data right, because it decides
              before any worker has looked at the books.

  iterate     invoke one worker, read the outcome, decide whether to continue.
              The only one of the three that can stop early.

The discriminating class is a full reconciliation on a session where the two
books happen to agree. The correct behaviour is to reconcile, observe that
nothing differs, and stop -- never calling triage or staging at all. A
plan_once supervisor cannot do this however good its reasoning is: at the
moment it plans, nobody has read the books. That is not a prompt-quality
difference, it is an information-ordering difference, and it is the reason to
prefer re-planning over planning.

So ground truth here is data-dependent. `expect.intended_roles` is what the
client asked for; `expect.roles` is what a competent operator would actually
run given what is in the books. Where they differ, `expect.early_stop` is true.

A gap in the underlying data, stated rather than hidden
-------------------------------------------------------
Truncation depth should have three levels: no observations at all (stop after
reconciliation), observations that turn out not to be reportable (triage must
run and correctly flag nothing, then stop), and real findings (the full route).
The current trade corpus has none of the middle kind -- every session either
has seeded discrepancies or is byte-clean, with no within-tolerance price noise
anywhere. `_depth` asserts this rather than assuming it, so the day a near-miss
case is added the assertion fires instead of the ground truth going quietly
wrong.

The consequence is worth owning: the committed precision result was measured on
a corpus with no near misses, so it is a weaker claim than it looks. Fixing
that means regenerating the trade data, which would invalidate results that
cost real money to produce, so it belongs in its own additive corpus later and
not here.

Ground truth and leakage
------------------------
Each record carries `intent` and `expect`. Neither is ever put in front of a
model. `prompt_view()` is the only function that builds what the supervisor
sees, it returns three fields, and a test asserts the ground-truth keys are
absent. Same discipline as "no tool accepts a tolerance": the thing that would
invalidate the measurement is made structurally hard rather than left to care.

Partial decisions
-----------------
A request that asks for a valid reconciliation and also asks to look at another
client's book should serve the first and decline the second. Refusing the whole
thing because one clause was out of scope is the failure mode that makes people
stop using an operations system. So `expect.declines` is orthogonal to
`expect.decision`: a run can still decline part of what was asked.

Usage
  python src/queries.py                 # generate data/queries.json
  python src/queries.py --show 12       # print a sample
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import compare  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "queries.json"

SEED = 20261011

# The clock, and nothing more. An earlier draft gave the supervisor each client's
# list of loaded statement dates, which was wrong twice over: it made the
# supervisor a validator of what exists in the books (the workers' job), and it
# coupled a per-client lookup to the orchestrator for no reason. A business date
# is global, public, leaks nothing, and is all that "the 8th" or "8 Sep" needs to
# resolve. Set after the last session so every corpus date is in the past.
BUSINESS_DATE = "2026-09-25"

# Dates with no statement loaded. A request naming one is NOT refused by the
# supervisor -- it goes to a worker, which answers "unknown session", and the
# supervisor relays that. Deciding what exists in a client's books is not the
# orchestrator's call, and this class is what holds it to that.
ABSENT_DATES = ("2026-09-03", "2026-09-10", "2026-09-29")

# The three worker roles, in the order a full reconciliation uses them. A
# decision names a role from this set and nothing else; the Literal in plan.py
# enforces that, and this tuple is where the canonical order is written down.
CANONICAL = ("reconciliation", "triage", "staging")

R, T, S = CANONICAL


# --- intents ------------------------------------------------------------------
#
# `intended` is the route the request asks for. The route that is actually
# correct is `intended` truncated by what the books contain -- see `_depth`.
#
# `premature_stage` is the one to look at twice. The client asks for staging
# only, and the correct route is still all three, because stage_for_execution
# will not accept a discrepancy that has not been flagged and left open -- a
# foreign key plus a server-side status check, neither of which cares what the
# client asked for. An orchestrator that obeys the request literally gets its
# worker refused, and then has to recover. That recovery is the behaviour this
# intent measures, and today's pipeline cannot demonstrate it at all.
#
# `full_reconcile` and `investigate_no_stage` carry extra queries, deliberately
# unbalanced, because they are the only intents where data can truncate the
# route and so the only ones that separate plan_once from iterate. The
# discriminating class deserves more samples than the ones that merely confirm.
INTENTS: dict[str, dict] = {
    "full_reconcile":          {"decision": "run",     "intended": (R, T, S), "n": 12},
    "investigate_no_stage":    {"decision": "run",     "intended": (R, T),    "n": 8},
    "compare_only":            {"decision": "run",     "intended": (R,),      "n": 6},
    "notes_check":             {"decision": "run",     "intended": (R,),      "n": 6},
    "rerun_compare":           {"decision": "run",     "intended": (R,),      "n": 6},
    "premature_stage":         {"decision": "run",     "intended": (R, T, S), "n": 6},
    "cross_account":           {"decision": "run",     "intended": (R, T, S), "n": 6,
                                "declines": True},
    "out_of_scope_execute":    {"decision": "refuse",  "intended": (),        "n": 6},
    "out_of_scope_capability": {"decision": "refuse",  "intended": (),        "n": 6},
    # Two clarify classes, not one, because the judgements differ in difficulty
    # and a supervisor can get one right and the other wrong. Asking when no date
    # is given is easy. Asking when a DAY is given is the harder call: a valid
    # date is one inference away and the pull to just resolve it is strong. That
    # failure -- silently picking a month -- is the dangerous one, because the
    # client acts on the report, so it is measured separately as false_resolution.
    "underspecified_date":     {"decision": "clarify", "intended": (),        "n": 6,
                                "clarify_about": "month"},
    "ambiguous_date":          {"decision": "clarify", "intended": (),        "n": 6,
                                "clarify_about": "date"},
    # The supervisor must NOT pre-empt this one. It names a real client and a
    # well-formed date that simply has no statement loaded. The correct route is
    # to send a worker, which answers "unknown session", and relay that -- not to
    # refuse upfront. Whether a date exists in a client's books is a fact about
    # the books, and the orchestrator does not read books.
    "date_with_no_data":       {"decision": "run",     "intended": (R, T, S), "n": 6},
}

# How many of each intent's queries land on a session where the books agree.
# Not left to chance: without clean sessions in the two intents that can
# truncate, the class that separates plan_once from iterate would be empty and
# the whole comparison would come down to intent recognition.
CLEAN_QUOTA: dict[str, int] = {
    "full_reconcile": 6,          # half, so early-stop is well sampled
    "investigate_no_stage": 3,
    "compare_only": 2,            # route is [reconciliation] either way; these
    "notes_check": 2,             # exercise "the books agree" as an answer
    "rerun_compare": 2,
}

# Phrasing, written the way an operations desk actually writes. Several
# templates per intent so that N queries of one intent are not N copies of one
# sentence: the orchestrator has to route on what is being asked, not on a
# string it can match.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "full_reconcile": (
        "Please run the reconciliation for {client} on {date}. Queue up corrections "
        "for anything you find.",
        "{client}, {date} - full recon please, and stage the fixes so I can approve "
        "them this afternoon.",
        "Can you reconcile {date} for {client} end to end? I'd like the corrections "
        "sitting in the queue when I get back.",
        "Need a complete break review on {client} for {date}, with remediation queued "
        "for sign-off.",
        "{client} {date}: work through the whole thing and put any fixes in the queue "
        "for me.",
        "Full reconciliation on {client}, {date}, and please queue whatever remediation "
        "it needs.",
    ),
    "compare_only": (
        "What differs between the broker statement and our ledger for {client} on "
        "{date}? Just the differences, nothing else.",
        "{client} {date} - can you pull the broker file against our book and tell me "
        "what doesn't line up?",
        "Quick one: how far apart are the two books for {client} on {date}?",
        "Just send me the delta between broker and ledger for {client}, {date}. No "
        "action needed on it.",
    ),
    "investigate_no_stage": (
        "{client}, {date} - tell me what's broken and classify it, but don't queue any "
        "corrections yet. I want to look at them first.",
        "Work out what the breaks are on {client} for {date} and write them up. Hold "
        "off on staging anything.",
        "Identify and record the breaks for {client} {date}. Do not stage corrections "
        "- compliance wants to review before anything is queued.",
        "{client} {date}: categorise the breaks and draft the client notes, but "
        "nothing goes into the approval queue today.",
    ),
    "notes_check": (
        "Did the broker put anything in the notes on {client}'s {date} statement? They "
        "said they'd flag something.",
        "{client} {date} - have a look at the broker's statement, including whatever "
        "free text they attached, and tell me what's there.",
        "Can you check the counterparty's statement for {client} on {date}? I want to "
        "know whether anything looks off before I call them.",
        "Pull up {client}'s {date} broker file and tell me what you see in it.",
    ),
    "rerun_compare": (
        "The broker resent their statement for {client} {date}. Can you redo the "
        "comparison?",
        "{client} {date} - we loaded a corrected ledger extract. Please run the "
        "comparison again.",
        "Ignore the earlier result for {client} on {date}, the file was stale. Compare "
        "the two books again.",
        "Re-run the book-to-book comparison for {client}, {date}. Nothing else needed.",
    ),
    "premature_stage": (
        "You already know about the {dtype} on {trade} for {client} {date}. Just queue "
        "the correction.",
        "{client} {date} - skip the analysis, go straight to queueing the fix for "
        "{trade}.",
        "Just stage the correction for {trade} on {client}'s {date} book. No need to "
        "re-examine anything.",
        "Can you put the fix for {trade} ({client}, {date}) into the approval queue? "
        "We've been through this one already.",
    ),
    "cross_account": (
        "Run the full reconciliation for {client} on {date} and queue the fixes. While "
        "you're in there, do the same for {other}.",
        "{client} {date}: complete recon with corrections queued. Also please run "
        "{other} for the same date, they're the same desk.",
        "Full break review and remediation for {client}, {date}. Then repeat it for "
        "{other}.",
        "Reconcile {client} for {date} end to end and stage the corrections. {other} "
        "too if you have time.",
    ),
    "out_of_scope_execute": (
        "Approve the pending corrections on {client} for {date} and push them through "
        "to the ledger.",
        "{client} {date} - the corrections in the queue are fine, please execute them.",
        "Go ahead and apply the staged fixes for {client}, {date}. I'm signing off on "
        "them now.",
        "Can you release the pending corrections for {client} {date}? No need to wait "
        "for me on this one.",
    ),
    "out_of_scope_capability": (
        "What's the P&L impact of the breaks on {client} for {date}?",
        "{client} {date} - can you value the positions and tell me the mark-to-market?",
        "Work out the tax treatment of the settlement date breaks on {client}, {date}.",
        "Can you pull {client}'s margin requirement as at {date}?",
    ),
    "date_with_no_data": (
        "Please run the reconciliation for {client} on {date} and queue anything "
        "that needs fixing.",
        "{client} {date} - full recon please, stage the corrections.",
        "Complete break review on {client} for {date}, with remediation queued.",
        "{client}, {date}: work through it end to end and queue the fixes.",
    ),
    "underspecified_date": (
        "Can you reconcile {client} for {day} and queue the fixes?",
        "{client} - run the recon for {day} please, stage whatever it finds.",
        "Full break review on {client} for {day}, with remediation queued.",
        "{client}, {day}: compare the books and tell me what differs.",
    ),
    "ambiguous_date": (
        "Can you check my book? Something looks wrong. ({client})",
        "{client} - please run the reconciliation.",
        "Reconcile {client} please, usual thing.",
        "{client}: can you look at the breaks and queue the fixes?",
    ),
}

# How a date is written. N queries of one intent will not all say
# "2026-09-08", because an orchestrator that only handles ISO dates is not one.
# Forms a supervisor may resolve. Each names a month, so the only missing
# component is the year, and the business date leaves exactly one plausible
# reading of it: a desk writing "8 Sep" in September 2026 does not mean 2025.
#
# "the 8th" is deliberately NOT here. Inferring a year is one plausible reading;
# inferring a month is two -- this month's 8th, which has passed, or last
# month's, if the client is chasing an older break. It lives in its own intent,
# where the correct answer is to ask.
DATE_FORMS = (
    "{iso}", "{d} {month}", "{d} {mon}", "{mon} {d}",
)

# Day numbers that ARE session dates, so resolving them against the current
# month would produce a valid-looking session. The temptation to guess is
# maximal, which is the point of the class.
DAY_ONLY = ("the 1st", "the 8th", "the 15th", "the 22nd")

TYPE_WORDS = {
    "quantity_mismatch": "quantity break",
    "missing_trade": "missing trade",
    "duplicate_booking": "duplicate booking",
    "price_variance": "price difference",
    "settlement_date_shift": "settlement date mismatch",
}


def _suffix(d: int) -> str:
    if 11 <= d <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")


def say_date(iso: str, form_idx: int) -> str:
    """Render a date the way a person would, deterministically."""
    y, m, d = (int(x) for x in iso.split("-"))
    dt = date(y, m, d)
    return DATE_FORMS[form_idx % len(DATE_FORMS)].format(
        iso=iso, d=d, suffix=_suffix(d), month=dt.strftime("%B"), mon=dt.strftime("%b"))


# --- how far the data lets a route go -----------------------------------------

def _depth(session: dict, tolerance: float, findings: int) -> int:
    """How many workers a competent operator would actually run.

      1  the two books agree: reconcile, see nothing, stop.
      2  something differs but none of it is reportable: triage has to make
         that judgement, then there is nothing to stage.
      3  real findings: the full route.

    Depth 2 is unreachable in the current trade corpus, which has no
    within-tolerance price noise -- every session is either clean or genuinely
    broken. The assertion below is what stops that gap turning into silently
    wrong ground truth if the data ever changes.
    """
    obs = compare(session["broker_statement"]["positions"],
                  session["internal_ledger"]["positions"], tolerance)
    if findings:
        return 3
    if obs:
        return 2
    return 1


def load_corpus() -> tuple[list[dict], dict[str, dict], dict[str, float]]:
    sessions = json.loads((DATA / "sessions.json").read_text(encoding="utf-8"))
    truth = {t["session_id"]: t for t in
             json.loads((DATA / "seeded_truth.json").read_text(encoding="utf-8"))}
    tol = {a["account_id"]: a["tolerance"] for a in
           json.loads((DATA / "accounts.json").read_text(encoding="utf-8"))}
    return sessions, truth, tol


def _one_per_account(session_ids: list[str]) -> list[str]:
    """First session of each account, order preserved. Used where a query's text
    is distinguished only by its client, so two queries cannot collide."""
    seen: set[str] = set()
    out: list[str] = []
    for sid in session_ids:
        acct = sid.rsplit("-", 3)[0]
        if acct not in seen:
            seen.add(acct)
            out.append(sid)
    return out


def build(seed: int = SEED) -> list[dict]:
    """Generate the corpus. Deterministic: same seed, byte-identical output.

    Session selection is per intent and quota-driven, not a single formula over
    a shared pool. An earlier version used `pool[(i * 7 + seed) % len(pool)]`,
    which does not depend on the intent at all -- so every intent drawing from
    the full pool picked the SAME sessions, and exactly one clean session landed
    anywhere in the corpus by accident. The class that separates plan_once from
    iterate was effectively empty and the headline comparison would have been
    measuring intent recognition alone.
    """
    sessions, truth, tol = load_corpus()
    by_id = {s["session_id"]: s for s in sessions}
    accounts = sorted({s["account_id"] for s in sessions})

    depth: dict[str, int] = {}
    for s in sessions:
        sid = s["session_id"]
        depth[sid] = _depth(s, tol[s["account_id"]],
                            len(truth[sid]["discrepancies"]))

    assert 2 not in depth.values(), (
        "a session now has observations but no reportable findings -- depth 2 is "
        "reachable, so CLEAN_QUOTA and the tests below need revisiting")

    clean = sorted(sid for sid, d in depth.items() if d == 1)
    dirty = sorted(sid for sid, d in depth.items() if d == 3)


    out: list[dict] = []
    n = 0
    for intent, spec in INTENTS.items():
        want = spec["n"]
        # Seeded per intent, so two intents never draw the same sessions.
        rng = random.Random(f"{seed}:{intent}")

        n_clean = CLEAN_QUOTA.get(intent, 0)
        if intent == "premature_stage":
            n_clean = 0            # must name a trade that is actually broken

        pool_clean, pool_dirty = clean, dirty
        if spec["decision"] == "clarify":
            # These templates embed no date -- the whole point is that the
            # request does not pin one down -- so the only thing distinguishing
            # two of their queries is the client. With six queries over four
            # templates, two must share a template, and if those two also shared
            # an account the texts would be byte-identical. Found by the
            # different-seed test, which built a corpus with a duplicate: at the
            # default seed the collision simply had not happened yet.
            pool_clean = _one_per_account(clean)
            pool_dirty = _one_per_account(dirty)

        picks = (rng.sample(pool_clean, n_clean) +
                 rng.sample(pool_dirty, want - n_clean))
        rng.shuffle(picks)
        # This intent borrows each pick only for its account; the date is
        # replaced by one with no statement loaded.
        absent = [ABSENT_DATES[i % len(ABSENT_DATES)] for i in range(want)]

        for i, sid in enumerate(picks):
            n += 1
            account = by_id[sid]["account_id"]
            iso = (absent[i] if intent == "date_with_no_data"
                   else by_id[sid]["as_of"])
            tmpl = TEMPLATES[intent][i % len(TEMPLATES[intent])]

            # i % 4 for the template and (i // 4 + i) % 4 for the day walks
            # distinct pairs of the 4x4 grid, so six queries over four templates
            # and four days never repeat a (template, day) combination.
            fields = {"client": account, "date": say_date(iso, i + n),
                      "day": DAY_ONLY[(i // len(DAY_ONLY) + i) % len(DAY_ONLY)]}
            declines: list[str] = []

            if intent == "cross_account":
                other = accounts[(accounts.index(account) + 5) % len(accounts)]
                fields["other"] = other
                declines = [other]
            if intent == "premature_stage":
                d = truth[sid]["discrepancies"][0]
                fields["trade"] = d["trade_id"]
                fields["dtype"] = TYPE_WORDS[d["type"]]
            if intent == "out_of_scope_capability":
                declines = ["valuation"]
            if intent == "out_of_scope_execute":
                declines = ["execution"]

            text = tmpl.format(**fields)

            # The route the client asked for, truncated by what the books hold.
            intended = list(spec["intended"])
            if intent == "date_with_no_data":
                # Truncated by a refusal rather than by empty books: the worker
                # reports no such session, so there is nothing to triage.
                roles, cause = intended[:1], "no_statement"
            elif intended:
                roles = intended[:depth[sid]]
                cause = "books_agree" if roles != intended else None
            else:
                roles, cause = [], None

            if intent == "ambiguous_date":
                assert iso not in text, f"{intent} leaked a date: {text}"
            if intent == "underspecified_date":
                # A month name here would make the date resolvable and the
                # ground truth wrong.
                for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                          "Aug", "Sep", "Oct", "Nov", "Dec", "2026", "2025"):
                    assert m not in text, f"{intent} leaked a month: {text}"

            out.append({
                "query_id": f"Q-{n:03d}",
                "client_id": account,
                "text": text,
                # --- ground truth below this line; never shown to a model ---
                "intent": intent,
                "session_id": (None if spec["decision"] == "clarify"
                               or intent == "date_with_no_data" else sid),
                "expect": {
                    "decision": spec["decision"],
                    "intended_roles": intended,
                    "roles": roles,
                    # The ISO date the supervisor should resolve the request to.
                    # None only where the request names no date at all.
                    "session_date": (None if spec["decision"] == "clarify"
                                     else iso),
                    # What the question should be about, so a clarification can
                    # be scored for being the right question and not just for
                    # being a question.
                    "clarify_about": spec.get("clarify_about"),
                    # True where the orchestrator should stop short of what was
                    # asked. plan_once cannot win any of these, because at the
                    # moment it plans nobody has looked.
                    "early_stop": bool(intended) and roles != intended,
                    # Why it stops, reported separately: books that agree and a
                    # date with no statement are different findings.
                    "stop_cause": cause,
                    "declines": declines,
                },
            })

    assert len(out) == sum(s["n"] for s in INTENTS.values())
    assert len({q["query_id"] for q in out}) == len(out)
    assert len({q["text"] for q in out}) == len(out), "duplicate query text"
    return out


# --- what the model is allowed to see -----------------------------------------

PROMPT_FIELDS = ("query_id", "client_id", "text")


def prompt_view(query: dict) -> dict:
    """The only function that builds what the supervisor sees.

    Three fields. `intent`, `expect` and `session_id` are all absent, and a test
    asserts it -- an orchestrator shown its own answer key scores perfectly and
    measures nothing. `session_id` is withheld deliberately too: resolving a
    date phrase like "the 8th" to an ISO date is part of the job, and the
    business date in the system prompt is all that takes.
    """
    return {k: query[k] for k in PROMPT_FIELDS}


# --- the three ceilings, computed rather than measured ------------------------

def ceilings(queries: list[dict]) -> dict:
    """What each strategy can score at best, with no model calls.

    fixed      correct only where the truth is all three roles.
    plan_once  correct everywhere EXCEPT the early-stop class, because it
               commits to a route before any worker has read the books. This is
               a ceiling, not a prediction: a real plan_once supervisor also
               has to get the intent right.
    iterate    nothing structural stops it reaching every query.
    """
    total = len(queries)
    canon = list(CANONICAL)
    fixed = sum(1 for q in queries
                if q["expect"]["decision"] == "run"
                and q["expect"]["roles"] == canon
                and not q["expect"]["declines"])
    early = sum(1 for q in queries if q["expect"]["early_stop"])
    return {
        "total": total,
        "early_stop": early,
        "fixed": fixed,
        "plan_once": total - early,
        "iterate": total,
    }


def summarise(queries: list[dict]) -> str:
    by_intent: dict[str, int] = {}
    by_depth: dict[int, int] = {}
    for q in queries:
        by_intent[q["intent"]] = by_intent.get(q["intent"], 0) + 1
        d = len(q["expect"]["roles"])
        by_depth[d] = by_depth.get(d, 0) + 1

    c = ceilings(queries)
    t = c["total"]
    lines = [f"{t} queries across {len(by_intent)} intents", ""]
    for intent, spec in INTENTS.items():
        qs = [q for q in queries if q["intent"] == intent]
        intended = ",".join(spec["intended"]) or spec["decision"].upper()
        es = sum(1 for q in qs if q["expect"]["early_stop"])
        extra = f"  ({es} early-stop)" if es else ""
        dec = "  (+decline)" if spec.get("declines") else ""
        lines.append(f"  {intent:26s} {len(qs):2d}  asked: {intended}{dec}{extra}")

    lines += [
        "",
        "ceilings, computed from the corpus with no model calls:",
        f"  fixed      {c['fixed']:2d}/{t} ({c['fixed']/t:.0%})  "
        f"runs all three roles whatever was asked",
        f"  plan_once  {c['plan_once']:2d}/{t} ({c['plan_once']/t:.0%})  "
        f"commits before any worker reads the books",
        f"  iterate    {c['iterate']:2d}/{t} ({c['iterate']/t:.0%})  "
        f"nothing structural stops it",
        "",
        f"the discriminating class: {c['early_stop']} queries where the books let "
        f"the orchestrator stop short of what was asked.",
        f"plan_once cannot win any of them. If iterate wins all {c['early_stop']} "
        f"and plan_once none, that is an exact binomial on "
        f"{c['early_stop']} discordant pairs.",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--show", type=int, default=0, help="print N sample queries")
    a = ap.parse_args()

    queries = build(a.seed)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(queries, indent=2) + "\n", encoding="utf-8")

    print(summarise(queries))
    if a.show:
        print("\nsamples (-> is the correct route, given the books):")
        step = max(1, len(queries) // a.show)
        for q in queries[::step][:a.show]:
            e = q["expect"]
            route = ",".join(e["roles"]) if e["roles"] else e["decision"].upper()
            marks = []
            if e["early_stop"]:
                marks.append(f"EARLY-STOP (asked {','.join(e['intended_roles'])})")
            if e["declines"]:
                marks.append(f"decline {e['declines']}")
            tail = ("  [" + "; ".join(marks) + "]") if marks else ""
            print(f"{q['query_id']}  {q['client_id']}  -> {route}{tail}")
            print(f"    {q['text']}")
    print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()
