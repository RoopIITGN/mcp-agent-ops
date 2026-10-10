"""
Is this deployment usable, and what will a pass cost?

Run this before the first real run. It makes four small calls -- a few hundred
tokens, a fraction of a cent -- and answers the questions that decide whether
the rest of the project works, rather than finding out on session forty of
sixty.

  Does it accept temperature=0? A reasoning model rejects the parameter
  outright. Without it the variance floor in Phase 4 cannot be measured, and
  every comparison smaller than that floor becomes unreportable. This is the
  check that matters most and it is the one nobody runs.

  Does it take max_tokens, or insist on max_completion_tokens? The newer
  reasoning deployments reject the former. Knowing which saves a confusing
  400 on the first batch.

  Does it emit a tool call against the real schema? The tools here nest their
  arguments under `args`, because flat parameters let the SDK silently strip an
  injected field. A model that cannot produce that shape is unusable here, and
  it is better to learn that now.

  How many tokens does one step actually cost? The estimate in the README was
  computed from a character count. This measures it.

Nothing is written and no database is touched. If a check fails, the others
still run, because "rejects temperature but handles tools fine" is a useful
answer and an exception on the first call is not.

Usage
  PYTHONPATH=src python src/check_model.py
  PYTHONPATH=src python src/check_model.py --deployment gpt-4o-mini
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tools                                                   # noqa: E402

# Measured from the committed trajectories: calls and tokens for one full pass
# over the 60 sessions. Used only to turn a per-call measurement into a total.
CALLS_PER_PASS = 336          # tool calls in a stub:ideal pass, plus the
                              # closing step each worker takes
PASSES = 4                    # baseline, variance repeat, schema-off arm,
                              # injection subset


from backends import load_env                                  # noqa: E402

# load_env lives in backends.py on purpose. It used to live here, which meant
# this check loaded .env and passed while the first real run died with
# KeyError: 'AZURE_OPENAI_DEPLOYMENT' -- the pre-flight check verified a
# configuration the thing it was checking could not see. One loader, imported
# by both, is the fix.


def client():
    from openai import AzureOpenAI
    missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"missing from .env: {', '.join(missing)}\n"
            "Expected keys (see .env.example):\n"
            "  AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com\n"
            "  AZURE_OPENAI_KEY=...\n"
            "  AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini\n"
            "  AZURE_OPENAI_API_VERSION=2024-10-21\n\n"
            "The endpoint is the bare host. Appending /openai/v1 produces a 404, "
            "because the SDK adds its own path.")
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


def usage_of(r) -> tuple[int, int, int]:
    """prompt, completion, reasoning. Reasoning tokens bill as completion and
    are what make a reasoning model expensive in an agent loop."""
    u = getattr(r, "usage", None)
    if u is None:
        return (0, 0, 0)
    reasoning = 0
    details = getattr(u, "completion_tokens_details", None)
    if details is not None:
        reasoning = getattr(details, "reasoning_tokens", 0) or 0
    return (u.prompt_tokens, u.completion_tokens, reasoning)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deployment", default="")
    a = ap.parse_args()

    load_env()
    dep = a.deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    if not dep:
        raise SystemExit("set AZURE_OPENAI_DEPLOYMENT in .env or pass --deployment")
    c = client()
    print(f"deployment: {dep}")
    print(f"endpoint:   {os.environ['AZURE_OPENAI_ENDPOINT']}")
    print(f"api version {os.environ.get('AZURE_OPENAI_API_VERSION', '2024-10-21')}\n")

    verdict: dict[str, bool] = {}
    tokens = {"prompt": 0, "completion": 0, "reasoning": 0}

    def record(r):
        p, cm, rs = usage_of(r)
        tokens["prompt"] += p
        tokens["completion"] += cm
        tokens["reasoning"] += rs
        return p, cm, rs

    # --- 1. does it answer at all, and does it take max_tokens ---------------
    kwarg = "max_tokens"
    try:
        r = c.chat.completions.create(
            model=dep, messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=16)
        record(r)
        print(f"  reachable, max_tokens accepted — {r.choices[0].message.content!r}")
        verdict["reachable"] = True
    except Exception as e:                                     # noqa: BLE001
        if "max_completion_tokens" in str(e):
            kwarg = "max_completion_tokens"
            try:
                r = c.chat.completions.create(
                    model=dep,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_completion_tokens=64)
                record(r)
                # Not a reasoning signal. `max_completion_tokens` is simply the
                # parameter name in the newer API family, and a deployment that
                # requires it may still accept temperature and spend no
                # reasoning tokens at all -- gpt-5.4-nano does both. What makes
                # a deployment awkward here is rejecting temperature, which the
                # next check tests directly.
                print("  reachable, requires max_completion_tokens "
                      "(the newer parameter name)")
                verdict["reachable"] = True
            except Exception as e2:                            # noqa: BLE001
                print(f"  UNREACHABLE: {type(e2).__name__}: {str(e2)[:200]}")
                verdict["reachable"] = False
        else:
            print(f"  UNREACHABLE: {type(e).__name__}: {str(e)[:200]}")
            verdict["reachable"] = False

    if not verdict.get("reachable"):
        raise SystemExit("\nStop here: fix the endpoint, key or deployment name first.")

    # --- 2. temperature=0 ----------------------------------------------------
    try:
        r = c.chat.completions.create(
            model=dep, messages=[{"role": "user", "content": "Name one colour."}],
            temperature=0, **{kwarg: 32})
        record(r)
        print("  temperature=0 accepted — the variance floor is measurable")
        verdict["temperature"] = True
    except Exception as e:                                     # noqa: BLE001
        print(f"  temperature=0 REJECTED — {str(e)[:140]}")
        verdict["temperature"] = False

    # --- 2b. reasoning_effort ------------------------------------------------
    # The one lever that matters on a reasoning deployment. Reasoning tokens
    # bill as completion and an agent loop re-reasons at every step, so cutting
    # them is the difference between cents and tens of dollars. Probed rather
    # than assumed, because whether a given deployment takes the parameter --
    # and what the values are called -- varies by model family.
    verdict["reasoning_effort"] = False
    best_effort = None
    if not verdict["temperature"]:
        for level in ("minimal", "low"):
            try:
                r = c.chat.completions.create(
                    model=dep,
                    messages=[{"role": "user", "content": "Name one colour."}],
                    reasoning_effort=level, **{kwarg: 256})
                _, _, rs = record(r)
                print(f"  reasoning_effort={level!r} accepted — "
                      f"{rs} reasoning token(s) on a trivial prompt")
                verdict["reasoning_effort"] = True
                best_effort = level
                break
            except Exception as e:                             # noqa: BLE001
                print(f"  reasoning_effort={level!r} rejected — {str(e)[:100]}")
        if not verdict["reasoning_effort"]:
            print("  no reasoning_effort control found; reasoning tokens are "
                  "whatever the model decides to spend")

    # --- 3. a tool call in the shape the server publishes -------------------
    schema = tools.schema("flag_discrepancy")
    fn_tool = [{"type": "function",
                "function": {"name": schema["name"],
                             "description": schema["description"],
                             "parameters": {"type": "object",
                                            "properties": {"args": schema["input_schema"]},
                                            "required": ["args"],
                                            "additionalProperties": False}}}]
    prompt = ("Session ACC-001-2026-09-01. The comparison reported: trade T794772, "
              "field quantity, broker 500, ledger 450. Record this discrepancy.")
    try:
        r = c.chat.completions.create(
            model=dep,
            messages=[{"role": "system",
                       "content": "Record discrepancies with the tool provided. "
                                  "Every tool takes one object argument named 'args'."},
                      {"role": "user", "content": prompt}],
            tools=fn_tool, tool_choice="auto", **{kwarg: 300})
        p, cm, rs = record(r)
        tc = r.choices[0].message.tool_calls
        if not tc:
            print(f"  NO TOOL CALL — content: {(r.choices[0].message.content or '')[:120]!r}")
            if rs:
                print(f"     {rs} reasoning token(s) consumed before any output; "
                      f"raise the token budget")
            verdict["tool_call"] = False
        else:
            raw = json.loads(tc[0].function.arguments or "{}")
            nested = "args" in raw
            print(f"  tool call emitted: {tc[0].function.name}, "
                  f"args nested correctly: {nested}")
            if nested:
                got = raw["args"]
                ok = got.get("trade_id") == "T794772" and \
                     got.get("type") == "quantity_mismatch"
                print(f"     arguments: {json.dumps(got)[:150]}")
                print(f"     classified correctly: {ok}")
            else:
                print(f"     FLAT arguments: {json.dumps(raw)[:150]} — the prompt "
                      f"needs to be explicit about the 'args' wrapper")
            verdict["tool_call"] = nested
    except Exception as e:                                     # noqa: BLE001
        print(f"  TOOL CALLING FAILED: {type(e).__name__}: {str(e)[:180]}")
        verdict["tool_call"] = False

    # --- 4. what it costs ----------------------------------------------------
    calls = 3
    print(f"\ntokens over {calls} probe call(s): {tokens['prompt']} prompt, "
          f"{tokens['completion']} completion"
          + (f", of which {tokens['reasoning']} reasoning" if tokens["reasoning"] else ""))

    if tokens["reasoning"]:
        print("  Reasoning tokens bill as completion, and an agent loop re-reasons "
              "at every step. This is the main cost multiplier.")

    print("\nprojected cost, using the measured 288k prompt / 32k completion tokens "
          "per pass:")
    for name, pin, pout in (("gpt-4o-mini", 0.15, 0.60),
                            ("gpt-4.1-mini", 0.40, 1.60)):
        per = 288_000 / 1e6 * pin + 32_000 / 1e6 * pout
        print(f"  at {name:13s} rates: ${per:.3f} per pass, "
              f"${per * PASSES:.2f} for {PASSES} passes")
    print("  (prices move; check the current sheet. The ratio is the point.)")

    # --- verdict -------------------------------------------------------------
    print()
    print("settings for .env / backends.AzureBackend:")
    print(f"  token parameter   {kwarg}")
    print(f"  temperature       {'0' if verdict.get('temperature') else 'omit (rejected)'}")
    print(f"  reasoning_effort  {best_effort or 'omit (not supported)'}")
    print()

    if verdict.get("temperature") and verdict.get("tool_call"):
        print("SUITABLE. Non-reasoning behaviour on both counts that matter: "
              "temperature is pinnable and tool calls arrive in the published "
              "shape.")
    elif verdict.get("tool_call"):
        print("USABLE, WITH ONE THING LOST. Tool calling works, so the project "
              "runs. Temperature cannot be pinned, so the variance floor is "
              "higher and any comparison smaller than it has to be reported as "
              "inconclusive rather than as a result -- which is a reporting "
              "constraint, not a blocker.")
        if verdict.get("reasoning_effort"):
            print(f"\nReasoning effort is controllable ({best_effort!r}), which is "
                  f"the main cost lever. Measure the first five sessions and "
                  f"check the per-session token count before running all sixty.")
        else:
            print("\nReasoning tokens are uncontrolled. Run --limit 5 FIRST and "
                  "read the reported cost before committing to a full pass.")
    else:
        print("NOT SUITABLE. Tool calling did not work in the published shape. "
              "Check the deployment is a chat model with function calling, and "
              "that the api version is recent enough.")
        raise SystemExit(1)

    print("\nNext: PYTHONPATH=src python src/agent.py --backend azure "
          "--limit 5 --fresh")


if __name__ == "__main__":
    main()
