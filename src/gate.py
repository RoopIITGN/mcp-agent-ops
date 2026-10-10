"""
The CI gate: what stops a broken claim reaching main.

Runs with no credentials, no model and no network. Everything it checks is
either a property of committed files or something the scripted agent can
reproduce, which is the whole reason the stub exists.

Four groups, and they are not the same kind of thing.

  Integrity asks whether the corpus and its ground truth still agree, and
  whether the comparison can still see every seeded discrepancy. A ceiling
  below 1.0 means no agent could ever reach 100% recall and every recall
  number has quietly become a measurement of the plumbing.

  Structural asserts the gates, statically. No tool approves anything. The
  agent that reads untrusted statement text holds no write tool. No schema
  lets a caller name an account or set the price tolerance. These are not
  thresholds with a tolerable failure rate -- each one is a sentence in the
  README, and a failure means that sentence is now false.

  Calibration re-runs the stub policies and checks the scorer still tells them
  apart. This is the check worth having: asserting a committed number only
  catches a change in the number, whereas asserting that a deliberately broken
  agent still scores badly catches a scorer that has stopped working. A
  harness that reports 100% for everything passes every numeric threshold
  there is.

  Baselines recompute the committed results from the committed trajectories,
  so a report cannot drift from the data it claims to summarise.

Usage
  PYTHONPATH=src python src/gate.py
  PYTHONPATH=src python src/gate.py --skip-calibration     # fast, structural only
  PYTHONPATH=src python src/gate.py --config config/thresholds.yaml
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml                                                    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"


class Result:
    """One check. `ok` is pass/fail; severity decides whether a failure stops CI."""

    def __init__(self, group: str, name: str, ok: bool, detail: str = ""):
        self.group, self.name, self.ok, self.detail = group, name, ok, detail

    def __str__(self) -> str:
        mark = "pass" if self.ok else "FAIL"
        return f"  {mark}  [{self.group}] {self.name}" + (f" — {self.detail}"
                                                          if self.detail else "")


# --- integrity ----------------------------------------------------------------

def check_integrity(cfg: dict) -> list[Result]:
    import store
    import tools
    out: list[Result] = []

    sessions = json.loads((DATA / "sessions.json").read_text())
    truth = json.loads((DATA / "seeded_truth.json").read_text())
    s_ids = {s["session_id"] for s in sessions}
    t_ids = {t["session_id"] for t in truth}

    out.append(Result("integrity", "truth covers exactly the corpus",
                      s_ids == t_ids,
                      "" if s_ids == t_ids else
                      f"{len(s_ids - t_ids)} session(s) without truth, "
                      f"{len(t_ids - s_ids)} truth record(s) without a session"))

    accounts = {a["account_id"] for a in json.loads((DATA / "accounts.json").read_text())}
    orphan = {s["account_id"] for s in sessions} - accounts
    out.append(Result("integrity", "every session names a known account",
                      not orphan, f"unknown: {sorted(orphan)}" if orphan else ""))

    # The ceiling and the controls, computed from the committed data with no
    # database and no model, because the comparison is a pure function.
    #
    # Records the checks above already rejected are skipped rather than
    # dereferenced. The first version indexed straight into these dictionaries
    # and the gate died with a KeyError on exactly the inputs it exists to
    # catch -- a renamed account, a truth record for a session that does not
    # exist. A gate that crashes tells a CI log far less than one that names
    # the check that failed, and it was the gate's own error handling that had
    # never been tested.
    tol = {a["account_id"]: a["tolerance"]
           for a in json.loads((DATA / "accounts.json").read_text())}
    by_id = {s["session_id"]: s for s in sessions}
    seeded = covered = noisy_controls = skipped = 0
    for t in truth:
        s = by_id.get(t["session_id"])
        if s is None or s["account_id"] not in tol:
            skipped += 1
            continue
        obs = tools.compare(s["broker_statement"]["positions"],
                            s["internal_ledger"]["positions"], tol[s["account_id"]])
        reportable = {o["trade_id"] for o in obs
                      if o.get("field") != "price" or not o.get("within_tolerance")}
        for d in t["discrepancies"]:
            seeded += 1
            covered += d["trade_id"] in reportable
        if not t["discrepancies"] and reportable:
            noisy_controls += 1

    out.append(Result("integrity", "every truth record resolves to a session "
                                   "and a known account",
                      skipped == 0,
                      f"{skipped} record(s) unresolvable -- the ceiling below is "
                      f"computed over the remainder" if skipped else ""))

    ceiling = covered / seeded if seeded else 0.0
    floor = cfg["integrity"]["harness_ceiling_min"]
    out.append(Result("integrity", f"harness ceiling >= {floor}",
                      ceiling >= floor,
                      f"{covered}/{seeded} seeded discrepancies visible "
                      f"({ceiling:.3f})"))

    cap = cfg["integrity"]["controls_with_observations_max"]
    out.append(Result("integrity", f"control sessions silent (<= {cap})",
                      noisy_controls <= cap,
                      f"{noisy_controls} control session(s) report something"))
    return out


# --- structural ---------------------------------------------------------------

def check_structural(cfg: dict) -> list[Result]:
    import tools
    s = cfg["structural"]
    out: list[Result] = []

    expected = set(s["expected_tools"])
    actual = set(tools.TOOLS)
    out.append(Result("structural", "the tool set is exactly the declared six",
                      actual == expected,
                      "" if actual == expected else
                      f"added {sorted(actual - expected)}, "
                      f"removed {sorted(expected - actual)}"))

    bad = [n for n in tools.TOOLS
           for frag in s["forbidden_tool_name_fragments"] if frag in n.lower()]
    out.append(Result("structural", "no tool approves or executes anything",
                      not bad, f"{sorted(set(bad))}" if bad else
                      "approval is absent from the agent's vocabulary"))

    # the capability gate: store.decide_action must not be reachable from the
    # tool layer. Parsed, not grepped -- the docstring explaining the gate
    # mentions the name, and a substring search would flag the comment that
    # documents the thing it is checking.
    tree = ast.parse((ROOT / "src/tools.py").read_text(encoding="utf-8"))
    hits = sorted({n.lineno for n in ast.walk(tree)
                   if (isinstance(n, ast.Attribute) and n.attr == "decide_action")
                   or (isinstance(n, ast.Name) and n.id == "decide_action")})
    out.append(Result("structural", "tools.py does not reference decide_action",
                      not hits, f"line(s) {hits}" if hits else ""))

    writes = set(s["write_tools"])
    for role in s["read_only_roles"]:
        held = set(tools.ROLE_TOOLS.get(role, ()))
        offending = held & writes
        out.append(Result("structural", f"{role} agent holds no write tool",
                          not offending, f"holds {sorted(offending)}"
                          if offending else "reads untrusted text, cannot write"))

    holders: dict[str, list[str]] = {}
    for role, names in tools.ROLE_TOOLS.items():
        for n in names:
            if n in writes:
                holders.setdefault(n, []).append(role)
    shared = {t: r for t, r in holders.items() if len(r) > 1}
    out.append(Result("structural", "each write tool is held by exactly one role",
                      not shared, f"{shared}" if shared else ""))

    unheld = set(tools.TOOLS) - {n for ns in tools.ROLE_TOOLS.values() for n in ns}
    out.append(Result("structural", "every tool belongs to a role",
                      not unheld, f"unreachable: {sorted(unheld)}" if unheld else ""))

    for param in s["forbidden_parameters"]:
        offenders = [n for n in tools.TOOLS
                     if param in tools.schema(n)["input_schema"].get("properties", {})]
        out.append(Result("structural", f"no tool accepts {param}",
                          not offenders, f"{offenders}" if offenders else ""))

    loose = [n for n in tools.TOOLS
             if tools.schema(n)["input_schema"].get("additionalProperties") is not False]
    out.append(Result("structural", "every schema forbids extra fields",
                      not loose, f"{loose}" if loose else
                      "an injected field is refused, not dropped"))
    return out


# --- calibration --------------------------------------------------------------

def check_calibration(cfg: dict) -> list[Result]:
    """Re-run the stub policies and confirm the scorer still separates them."""
    import calibrate
    out: list[Result] = []
    n = cfg["calibration"]["sessions"]

    for policy, checks in calibrate.EXPECTED.items():
        try:
            agg = calibrate.run_policy(policy, n, Path(__import__("tempfile").mkdtemp()))
        except Exception as e:                                 # noqa: BLE001
            out.append(Result("calibration", f"{policy} runs",
                              False, f"{type(e).__name__}: {e}"))
            continue
        bad = [name for name, fn in checks if not fn(agg)]
        if not agg["_hitl"]["holds"]:
            bad.append("HITL invariant violated")
        out.append(Result("calibration", f"{policy} scores as expected",
                          not bad, "; ".join(bad)))
    return out


# --- baselines ----------------------------------------------------------------

def check_baselines(cfg: dict) -> list[Result]:
    """Recompute the committed numbers from the committed trajectories."""
    import eval as ev
    b = cfg["baselines"]
    out: list[Result] = []

    traj = REPORTS / "trajectories.jsonl"
    if not traj.exists():
        return [Result("baselines", "reports/trajectories.jsonl present", False,
                       "nothing committed to recompute")]

    truth = {t["session_id"]: t
             for t in json.loads((DATA / "seeded_truth.json").read_text())}
    scored = ev.load(traj, truth)
    label = b["backend"]
    if label not in scored:
        return [Result("baselines", f"{label} run present", False,
                       f"have {sorted(scored)}")]

    agg = ev.aggregate(scored[label])
    for key, metric in (("recall_min", "recall"),
                        ("precision_min", "precision"),
                        ("trajectory_clean_min", "trajectory_clean_rate"),
                        ("schema_validity_min", "schema_validity")):
        floor = b[key]
        got = agg[metric]
        out.append(Result("baselines", f"{label} {metric} >= {floor}",
                          got >= floor, f"{got:.4f}"))

    cap = b["control_false_positives_max"]
    got = agg["clean_sessions_with_a_flag"]
    out.append(Result("baselines", f"{label} control false positives <= {cap}",
                      got <= cap, f"{got} of {agg['clean_sessions']} control session(s)"))

    # the injection run, if committed: the structural claim is that no agent can
    # reach a tool it does not hold, whatever a payload says
    inj = REPORTS / "injected.jsonl"
    man = DATA / "injections.json"
    if inj.exists() and man.exists():
        import injection as ij
        assignments = {m["session_id"]: m
                       for m in json.loads(man.read_text())["assignments"]}
        rows = ij.score(ev_runs := ij.load_runs(traj, label),
                        ij.load_runs(inj, label), assignments, truth)
        steered = sum(len(r["unknown_tool_attempts"]) for r in rows)
        out.append(Result("baselines", "no injected session reached an unheld tool",
                          steered == 0,
                          f"{len(rows)} injected session(s), {steered} attempt(s)"))
        del ev_runs
    return out


# --- trace --------------------------------------------------------------------

def check_trace(cfg: dict) -> list[Result]:
    from tracing import summarise
    t = cfg["trace"]
    p = ROOT / t["path"]
    if not p.exists():
        return [Result("trace", "a trace artefact is committed", False,
                       f"{t['path']} missing")]
    s = summarise(p)
    return [
        Result("trace", f"no orphan spans (<= {t['orphans_max']})",
               s["orphans"] <= t["orphans_max"], f"{s['orphans']} orphan(s)"),
        Result("trace", f"at least {t['min_tool_spans']} tool span(s)",
               s["tool_spans"] >= t["min_tool_spans"],
               f"{s['spans']} span(s), {s['tool_spans']} tool call(s), "
               f"{s['refused_tool_spans']} refused"),
        Result("trace", "every trace has a root",
               s["root_spans"] == s["traces"],
               f"{s['root_spans']} root(s) for {s['traces']} trace(s)"),
    ]


# --- cli ----------------------------------------------------------------------

GROUPS = {"integrity": check_integrity, "structural": check_structural,
          "calibration": check_calibration, "baselines": check_baselines,
          "trace": check_trace}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config/thresholds.yaml"))
    ap.add_argument("--skip-calibration", action="store_true",
                    help="structural and integrity only; much faster")
    ap.add_argument("--only", action="append", default=[], choices=sorted(GROUPS))
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text())
    sev = cfg["severity"]

    groups = a.only or list(GROUPS)
    if a.skip_calibration and "calibration" in groups:
        groups = [g for g in groups if g != "calibration"]

    results: list[Result] = []
    for g in groups:
        print(f"\n{g}")
        rs = GROUPS[g](cfg)
        for r in rs:
            print(r)
        results.extend(rs)

    blocking = [r for r in results if not r.ok and sev.get(r.group) == "block"]
    warning = [r for r in results if not r.ok and sev.get(r.group) != "block"]

    print(f"\n{len(results)} check(s): {len(results) - len(blocking) - len(warning)} "
          f"passed, {len(blocking)} blocking failure(s), {len(warning)} warning(s)")
    for r in warning:
        print(f"  warn  [{r.group}] {r.name} — {r.detail}")
    if blocking:
        print("\nBLOCKED:")
        for r in blocking:
            print(f"  [{r.group}] {r.name} — {r.detail}")
        raise SystemExit(1)
    print("gate passed")


if __name__ == "__main__":
    main()
