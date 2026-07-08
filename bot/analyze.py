#!/usr/bin/env python3
"""Tier 3 — the weekly CORE researcher.

Runs the TradingAgents multi-agent stack (deepresearch.research, profile=core)
on the small long-term core watchlist and turns each rating into a CORE-sleeve
intent. Those intents flow through the same guardrails.validate_intents path as
the aggressive sleeve, so the 20% core cap, the `core` hold-class (12% max pos,
5-day hold, 15% stop) and all account safety rules are enforced by the one
order author. Each decision is also written to the TradingAgents decision memory
so reflect.py can later attach the outcome and the brain can learn from it.

The aggressive leveraged sleeve is NOT handled here — the intraday daemon owns
it. This script only produces the core book's intents.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT_DIR))
import deepresearch

ET = ZoneInfo("America/New_York")

# rating -> base conviction fraction of equity for a core entry (vol-target and
# the core 12% class cap further bound this in guardrails).
CORE_TARGET = {"Buy": 0.12, "Overweight": 0.08}
CORE_STOP_PCT = 0.15
CORE_POLICY = "core_research"


def log(msg):
    print(f"[core {datetime.now(ET).isoformat(timespec='seconds')}] {msg}", flush=True)


def rating_to_intent(ticker, rating):
    """Map a 5-tier research rating to a core-sleeve intent, or None for Hold."""
    if rating in CORE_TARGET:
        return {"ticker": ticker, "side": "buy", "policy_id": CORE_POLICY,
                "sleeve": "core", "hold_class": "core",
                "target_frac": CORE_TARGET[rating], "stop_pct": CORE_STOP_PCT,
                "realized_vol": None, "reason": f"core research: {rating}"}
    if rating in ("Sell", "Underweight"):
        return {"ticker": ticker, "side": "sell", "policy_id": CORE_POLICY,
                "sleeve": "core", "hold_class": "core",
                "reason": f"core research: {rating}"}
    return None


def load_core_watchlist():
    u = json.loads((BOT_DIR / "universe.json").read_text())
    return u.get("core_watchlist") or u.get("tickers", [])[:5]


def store_memory(ticker, date, info):
    """Append a pending decision to the TradingAgents decision memory."""
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        mem = TradingMemoryLog({"memory_log_path": str(BOT_DIR / "logs" / "decision_memory.md"),
                                "memory_log_max_entries": 500})
        if info.get("final_decision"):
            mem.store_decision(ticker, date, info["final_decision"])
    except Exception as e:
        log(f"memory store failed for {ticker}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="decisions.json path")
    ap.add_argument("--tickers", nargs="*", help="override core watchlist (testing)")
    args = ap.parse_args()

    today = datetime.now(ET).strftime("%Y-%m-%d")
    tickers = args.tickers or load_core_watchlist()
    log(f"core research over {tickers}")

    intents = []
    for t in tickers:
        info = deepresearch.research(t, today, profile="core")
        log(f"{t}: {info['rating']} (ok={info['ok']})")
        store_memory(t, today, info)
        it = rating_to_intent(t, info["rating"])
        if it:
            intents.append(it)

    doc = {
        "slot": "core",
        "date": today,
        "generated_at": datetime.now(ET).isoformat(),
        "intents": intents,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, indent=2))
    log(f"wrote {args.out}: {len(intents)} core intents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
