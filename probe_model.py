#!/usr/bin/env python3
"""
What does this deployment actually accept? One parameter per call, no guessing.

Standalone on purpose: it imports nothing from the project, so it can be dropped
anywhere and run against any deployment without the repo being in a consistent
state. Its only dependency is `openai`.

Why it exists

The project's backend adapts to rejections by matching substrings in the error
message -- "temperature" in the text means temperature was the problem. That is
fine for recovering mid-run and useless for finding out what a deployment
supports, for two reasons:

  it changes several things at once. The first probe sent max_tokens AND
  temperature AND a forced tool call, so a 400 could be blamed on the wrong one;

  an error that merely mentions a parameter is not an error about it. A message
  listing several unsupported options contains the word "temperature" whether or
  not temperature is the problem.

So every probe here sends a known-good base call plus exactly ONE change, and
prints the API's own message rather than an interpretation of it. A rejection
names its cause because nothing else varied.

Cost: about fifteen calls per deployment, 16 output tokens each. Fractions of a
cent. Run it whenever a deployment is created or behaves unexpectedly -- these
answers change under you. A deployment here accepted temperature=0 one day and
rejected it the next, same name, same .env.

Usage
  python probe_model.py                          # the deployment in .env
  python probe_model.py gpt-5.4-nano gpt-5.4-mini
  python probe_model.py --env ~/projects/mcp-agent-ops/.env gpt-5.4-mini
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROMPT = [{"role": "user", "content": "Say OK."}]
BUDGET = 16          # plain probes
TOOL_BUDGET = 256    # a tool call needs room, and a reasoning model more

# A tool shaped exactly like the project's: arguments nested under `args`, with
# additionalProperties false. A deployment that cannot produce this shape cannot
# run the project, however well it does on everything else.
TOOL = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "Record one orchestration step.",
        "parameters": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["invoke", "done", "refuse"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["action", "reason"],
                    "additionalProperties": False,
                }
            },
            "required": ["args"],
            "additionalProperties": False,
        },
    },
}


def load_env(path: Path) -> None:
    """Minimal .env reader. setdefault, so a shell variable wins over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def short(e: Exception) -> str:
    """The API's own message, unwrapped from the SDK's envelope where possible."""
    text = str(e)
    start = text.find("{")
    if start != -1:
        try:
            body = json.loads(text[start:].replace("'", '"'))
            msg = body.get("error", {}).get("message")
            if msg:
                return msg
        except Exception:                                      # noqa: BLE001
            pass
    return text.replace("\n", " ")[:220]


def probe(client, deployment: str, label: str, **extra) -> tuple[bool, str, dict]:
    """One call: the base plus exactly `extra`. Returns (accepted, detail, usage)."""
    call = {"model": deployment, "messages": PROMPT}
    call.update(extra)
    try:
        r = client.chat.completions.create(**call)
    except Exception as e:                                     # noqa: BLE001
        return False, short(e), {}

    usage = {}
    if r.usage:
        usage = {"prompt": r.usage.prompt_tokens,
                 "completion": r.usage.completion_tokens}
        details = getattr(r.usage, "completion_tokens_details", None)
        rt = getattr(details, "reasoning_tokens", None) if details else None
        if rt is not None:
            usage["reasoning"] = rt

    choice = r.choices[0]
    calls = choice.message.tool_calls or []
    if calls:
        try:
            args = json.loads(calls[0].function.arguments or "{}")
        except json.JSONDecodeError:
            return True, "tool call returned unparseable arguments", usage
        if "args" not in args:
            return True, f"tool call NOT nested under args: {sorted(args)}", usage
        return True, f"tool call nested correctly: {sorted(args['args'])}", usage

    return True, (choice.message.content or "").strip()[:60] or "(empty)", usage


# The project's supervisor call, reproduced shape for shape: a long system
# prompt, a long user message, a forced tool call, a 4000-token completion
# budget and temperature=0. This exists because the probe above said
# temperature=0 was accepted on a deployment that had rejected it an hour
# earlier through the project's backend. Only one of those can be true of a
# given call, so the call is reproduced rather than reasoned about.
REPLICA_SYSTEM = (
    "You orchestrate a reconciliation operations system for one client at a "
    "time. You hold no tools and cannot read any client's books. You decide, "
    "one step at a time, which worker to invoke next or whether to stop. "
) * 3
REPLICA_USER = (
    "The three workers you may invoke, one per decision: reconciliation, "
    "triage, staging. Today is 2026-09-25. Request Q-001 from ACC-003: please "
    "run the reconciliation for ACC-003 on 8 September and queue up "
    "corrections for anything you find. What has happened so far: nothing. "
) * 6


def replicate(client, deployment: str, token_param: str) -> None:
    """Does the project's exact call shape behave like the small probes?"""
    print("\n  the project's call, reproduced")
    tool_call = {
        "tools": [TOOL],
        "tool_choice": {"type": "function",
                        "function": {"name": "submit_decision"}},
    }
    cases = [
        ("budget 4000, no temperature", {token_param: 4000, **tool_call}),
        ("budget 4000 + temperature=0",
         {token_param: 4000, "temperature": 0, **tool_call}),
        ("budget 900 + temperature=0",
         {token_param: 900, "temperature": 0, **tool_call}),
    ]
    for label, extra in cases:
        call = {"model": deployment,
                "messages": [{"role": "system", "content": REPLICA_SYSTEM},
                             {"role": "user", "content": REPLICA_USER}]}
        call.update(extra)
        try:
            r = client.chat.completions.create(**call)
            calls = r.choices[0].message.tool_calls or []
            detail = ("tool call returned" if calls
                      else f"no tool call: {(r.choices[0].message.content or '')[:40]!r}")
            print(f"    ok   {label:28s} {detail}")
        except Exception as e:                                 # noqa: BLE001
            print(f"    no   {label:28s} {short(e)}")
    print("    If these disagree with the small probes above, the difference is "
          "the budget or the prompt, not the deployment. If they agree, the "
          "deployment's behaviour changed since the last run.")


def run(deployment: str, client) -> dict:
    print(f"\n{'=' * 72}\n{deployment}\n{'=' * 72}")
    found: dict = {"deployment": deployment}

    # --- 1. which token parameter, established before anything else ----------
    # Everything downstream needs a base call that works, or every later
    # rejection is ambiguous.
    token_param = None
    for name in ("max_tokens", "max_completion_tokens"):
        ok, detail, _ = probe(client, deployment, name, **{name: BUDGET})
        print(f"  {'ok  ' if ok else 'no  '} {name:26s} {detail}")
        if ok:
            token_param = name
            break
    if token_param is None:
        print("  unreachable, or neither token parameter is accepted. Stop here.")
        found["reachable"] = False
        return found

    found["reachable"] = True
    found["token_param"] = token_param
    base = {token_param: BUDGET}
    print(f"\n  base call: {{{token_param}: {BUDGET}}}\n")

    # --- 2. one parameter at a time ------------------------------------------
    print("  sampling controls")
    for label, extra in (
        ("temperature=0", {"temperature": 0}),
        ("temperature=0.2", {"temperature": 0.2}),
        ("temperature=1", {"temperature": 1}),
        ("top_p=0.1", {"top_p": 0.1}),
        ("seed=7", {"seed": 7}),
    ):
        ok, detail, _ = probe(client, deployment, label, **base, **extra)
        print(f"    {'ok  ' if ok else 'no  '} {label:18s} {detail}")
        found[label] = ok

    print("\n  reasoning effort")
    for level in ("minimal", "low", "medium", "high"):
        label = f"reasoning_effort={level}"
        ok, detail, usage = probe(client, deployment, label,
                                  **{token_param: TOOL_BUDGET},
                                  reasoning_effort=level)
        rt = usage.get("reasoning")
        note = f"  [{rt} reasoning tokens]" if rt is not None else ""
        print(f"    {'ok  ' if ok else 'no  '} {label:26s} {detail}{note}")
        found[label] = ok
        if ok and rt is not None:
            found[f"reasoning_tokens_{level}"] = rt

    # --- 3. the tool path, and whether it changes the answers ---------------
    # This is where the earlier confusion came from: the project's supervisor
    # forces a tool call, and the suspicion was that temperature is only
    # rejected on that path. Tested directly rather than inferred.
    print("\n  forced tool call")
    tool_base = {token_param: TOOL_BUDGET, "tools": [TOOL],
                 "tool_choice": {"type": "function",
                                 "function": {"name": "submit_decision"}}}
    ok, detail, usage = probe(client, deployment, "tool_choice forced",
                              **tool_base)
    print(f"    {'ok  ' if ok else 'no  '} {'forced tool_choice':26s} {detail}")
    found["tool_choice"] = ok
    found["tool_nesting_ok"] = ok and "nested correctly" in detail

    if found.get("temperature=0") and ok:
        ok2, detail2, _ = probe(client, deployment, "both", **tool_base,
                                temperature=0)
        print(f"    {'ok  ' if ok2 else 'no  '} "
              f"{'+ temperature=0':26s} {detail2}")
        found["temperature_with_tools"] = ok2
    elif ok and not found.get("temperature=0"):
        print("    --   + temperature=0             skipped: rejected on its own, "
              "so the tool path is not the cause")
        found["temperature_with_tools"] = False

    if found.get(f"reasoning_effort=low") and ok:
        ok3, detail3, usage3 = probe(client, deployment, "tools+reasoning",
                                     **tool_base, reasoning_effort="low")
        rt = usage3.get("reasoning")
        note = f"  [{rt} reasoning tokens]" if rt is not None else ""
        print(f"    {'ok  ' if ok3 else 'no  '} "
              f"{'+ reasoning_effort=low':26s} {detail3}{note}")
        found["reasoning_with_tools"] = ok3

    replicate(client, deployment, token_param)
    return found


def verdict(f: dict) -> None:
    d = f["deployment"]
    print(f"\n  {d}:")
    if not f.get("reachable"):
        print("    not reachable")
        return

    pinnable = f.get("temperature=0") and f.get("temperature_with_tools", True)
    if pinnable:
        print("    temperature CAN be pinned at 0 -- a near-zero variance floor "
              "is measurable, so a small difference between two arms is "
              "reportable.")
    elif f.get("temperature=0") and not f.get("temperature_with_tools", True):
        print("    temperature=0 works alone but NOT with a forced tool call. "
              "The supervisor forces one, so it runs unpinned.")
    else:
        print("    temperature CANNOT be pinned. Every run samples, so two "
              "measurements of the same arm will differ and the variance floor "
              "has to be measured before any comparison means anything.")

    levels = [lv for lv in ("minimal", "low", "medium", "high")
              if f.get(f"reasoning_effort={lv}")]
    if levels:
        counts = {lv: f.get(f"reasoning_tokens_{lv}") for lv in levels}
        spent = {k: v for k, v in counts.items() if v}
        print(f"    reasoning_effort accepted at: {', '.join(levels)}")
        if spent:
            print(f"    and it actually spends reasoning tokens: {spent}")
            print(f"    USE ONE OF THESE LEVELS for a reasoning arm: "
                  f"{', '.join(sorted(spent))}. The levels reporting zero are "
                  f"accepted and do nothing, so an arm set to one of those "
                  f"would be the same model under a different label -- a "
                  f"comparison of nothing against nothing.")
        else:
            print("    but it reports ZERO reasoning tokens at every level. The "
                  "parameter is accepted and appears to do nothing, so a "
                  "'reasoning arm' would be the same model at a different "
                  "label. Say so rather than reporting it as a comparison.")
    else:
        print("    reasoning_effort rejected -- no reasoning arm on this "
              "deployment.")

    if not f.get("tool_choice"):
        print("    UNUSABLE: it cannot be forced to call a function, so the "
              "supervisor cannot be made to answer with a decision.")
    elif not f.get("tool_nesting_ok"):
        print("    UNUSABLE: the tool call did not nest arguments under `args`, "
              "which is what makes an injected field a validation error "
              "instead of a silently dropped one.")
    else:
        print("    forced tool calls work and nest correctly.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deployments", nargs="*", default=[])
    ap.add_argument("--env", default=".env")
    a = ap.parse_args()

    load_env(Path(a.env).expanduser())
    missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"missing from {a.env} or the environment: {', '.join(missing)}")
        return 1

    deployments = a.deployments or [os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")]
    deployments = [d for d in deployments if d]
    if not deployments:
        print("no deployment given and AZURE_OPENAI_DEPLOYMENT is not set")
        return 1

    from openai import AzureOpenAI
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    print(f"endpoint: {os.environ['AZURE_OPENAI_ENDPOINT']}")
    print(f"api_version: "
          f"{os.environ.get('AZURE_OPENAI_API_VERSION', '2024-10-21')}")

    results = [run(d, client) for d in deployments]

    print(f"\n{'=' * 72}\nwhat to do with this\n{'=' * 72}")
    for f in results:
        verdict(f)

    usable = [f for f in results
              if f.get("reachable") and f.get("tool_nesting_ok")]
    if len(usable) > 1:
        pin = [f["deployment"] for f in usable
               if f.get("temperature=0") and f.get("temperature_with_tools", True)]
        print("\n  choosing between them:")
        if pin:
            print(f"    {', '.join(pin)} can pin temperature. Prefer one of "
                  f"those for the arm you want to compare against something "
                  f"else -- an unpinned arm needs its own variance floor "
                  f"measured first, which costs an extra run.")
        else:
            print("    none can pin temperature, so whichever you pick needs a "
                  "repeat run to establish the variance floor before any "
                  "comparison is reportable.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
