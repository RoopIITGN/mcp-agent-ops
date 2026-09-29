"""
Generate broker statements and internal ledgers with discrepancies seeded
deliberately, so every later measurement has ground truth for free.

This runs before the server and before the agent, because everything downstream
scores against it. In the retrieval project the labels cost four hours of
hand-verification; here they are a by-product of generating the data, which is
the one real advantage synthetic data has.

Two structural decisions worth keeping.

  Accounts are few and sessions are many. Fifteen accounts reconciled across
  four as-of dates gives sixty trajectories -- the unit that tool-selection
  accuracy is measured over. Ten sessions would make a single mistake worth ten
  percentage points and nothing would clear a significance test.

  Roughly a third of sessions are CLEAN, with nothing wrong. An agent that
  flags something every time looks perfect until you check the controls, and
  without them the false-positive rate is unmeasurable.

The broker statement carries a free-text `notes` field that is empty here. It
exists so the injection suite can fill it later without changing the schema --
the realistic attack is untrusted text inside a document the agent reads.

Usage
  python src/generate_data.py            # 15 accounts x 4 dates
  python src/generate_data.py --accounts 15 --dates 4 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data"

INSTRUMENTS = [
    ("RELIANCE", 1400.0), ("HDFCBANK", 1650.0), ("INFY", 1850.0),
    ("TCS", 4100.0), ("ICICIBANK", 1250.0), ("SBIN", 820.0),
    ("BHARTIARTL", 1580.0), ("ITC", 470.0), ("LT", 3600.0), ("AXISBANK", 1140.0),
]

# Every kind the agent is expected to find, plus the control.
KINDS = ["quantity_mismatch", "missing_trade", "duplicate_booking",
         "price_variance", "settlement_date_shift"]
CLEAN_SHARE = 0.33
PRICE_TOLERANCE = 0.005          # 0.5%; the agent is told this, so it is checkable


def trade(rng: random.Random, tid: str, as_of: date) -> dict:
    sym, base = rng.choice(INSTRUMENTS)
    return {
        "trade_id": tid,
        "instrument": sym,
        "quantity": rng.choice([100, 250, 500, 1000, 2500]),
        "price": round(base * rng.uniform(0.97, 1.03), 2),
        "settlement_date": (as_of + timedelta(days=2)).isoformat(),
    }


def seed_discrepancy(rng: random.Random, broker: list[dict], ledger: list[dict],
                     kind: str, used: set[str]) -> dict | None:
    """Mutate the ledger away from the broker statement, and describe how.

    One trade carries at most one discrepancy. Seeding two on the same trade
    produces truth that contradicts the data -- a trade cannot be both removed
    and duplicated -- and the contradiction is silent, which is the worst kind.
    """
    free = [t for t in broker if t["trade_id"] not in used]
    if not free:
        return None
    src = rng.choice(free)
    used.add(src["trade_id"])
    target = next(t for t in ledger if t["trade_id"] == src["trade_id"])

    if kind == "quantity_mismatch":
        before = target["quantity"]
        target["quantity"] = max(50, before - rng.choice([50, 100, 250]))
        detail = {"broker_quantity": before, "ledger_quantity": target["quantity"]}

    elif kind == "missing_trade":
        ledger.remove(target)
        detail = {"broker_quantity": src["quantity"], "ledger_quantity": 0}

    elif kind == "duplicate_booking":
        ledger.append({**target})
        detail = {"ledger_occurrences": 2}

    elif kind == "price_variance":
        before = target["price"]
        target["price"] = round(before * rng.choice([1.02, 0.98, 1.035]), 2)
        detail = {"broker_price": before, "ledger_price": target["price"],
                  "tolerance": PRICE_TOLERANCE}

    elif kind == "settlement_date_shift":
        before = target["settlement_date"]
        shifted = date.fromisoformat(before) + timedelta(days=1)
        target["settlement_date"] = shifted.isoformat()
        detail = {"broker_settlement": before, "ledger_settlement": shifted.isoformat()}

    else:
        raise ValueError(kind)

    return {"type": kind, "trade_id": src["trade_id"],
            "instrument": src["instrument"], **detail}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=15)
    ap.add_argument("--dates", type=int, default=4)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    start = date(2026, 9, 1)
    OUT.mkdir(parents=True, exist_ok=True)

    accounts = [{"account_id": f"ACC-{i:03d}",
                 "name": f"Client {i:03d}",
                 "tolerance": PRICE_TOLERANCE}
                for i in range(1, a.accounts + 1)]

    sessions, truth = [], []
    for d in range(a.dates):
        as_of = start + timedelta(days=d * 7)
        for acct in accounts:
            sid = f"{acct['account_id']}-{as_of.isoformat()}"
            n = rng.randint(4, 9)
            broker = [trade(rng, f"T{rng.randrange(10**6):06d}", as_of) for _ in range(n)]
            ledger = [{**t} for t in broker]

            found: list[dict] = []
            if rng.random() >= CLEAN_SHARE:
                used: set[str] = set()
                for kind in rng.sample(KINDS, rng.choice([1, 1, 2])):
                    d = seed_discrepancy(rng, broker, ledger, kind, used)
                    if d:
                        found.append(d)

            sessions.append({
                "session_id": sid, "account_id": acct["account_id"],
                "as_of": as_of.isoformat(),
                "broker_statement": {"account_id": acct["account_id"],
                                     "as_of": as_of.isoformat(),
                                     "positions": broker,
                                     "notes": ""},      # injection lands here later
                "internal_ledger": {"account_id": acct["account_id"],
                                    "as_of": as_of.isoformat(),
                                    "positions": ledger},
            })
            truth.append({"session_id": sid, "account_id": acct["account_id"],
                          "as_of": as_of.isoformat(), "discrepancies": found})

    (OUT / "accounts.json").write_text(json.dumps(accounts, indent=2))
    (OUT / "sessions.json").write_text(json.dumps(sessions, indent=2))
    (OUT / "seeded_truth.json").write_text(json.dumps(truth, indent=2))

    clean = sum(1 for t in truth if not t["discrepancies"])
    total = sum(len(t["discrepancies"]) for t in truth)
    by_kind: dict[str, int] = {}
    for t in truth:
        for d in t["discrepancies"]:
            by_kind[d["type"]] = by_kind.get(d["type"], 0) + 1

    print(f"{len(accounts)} accounts x {a.dates} dates = {len(sessions)} sessions")
    print(f"  clean sessions : {clean} ({100*clean/len(sessions):.0f}%)")
    print(f"  discrepancies  : {total}")
    for k in KINDS:
        print(f"      {k:24s} {by_kind.get(k, 0)}")
    print(f"\n-> {OUT}/sessions.json, seeded_truth.json, accounts.json")


if __name__ == "__main__":
    main()
