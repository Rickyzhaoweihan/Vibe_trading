#!/usr/bin/env python3
"""Reflection layer — the bot's self-tuning loop.

Reads the policy_id-tagged trade log, attributes realized P&L to each policy,
and proposes weight / enable changes. reflect.py is the SOLE writer of
policies.json (kept separate from state.json so no file has two writers).

Pure functions (compute_attribution, propose_updates, apply_updates) are
fully unit-testable. main() wires them to the files. In Phase 2 it runs in
LOG-ONLY mode (--apply off): it prints what it *would* change. Phase 4 turns
on --apply with the safety rails already encoded here (min trade count, capped
per-cycle weight delta, full change_log, notify on every change).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))
sys.path.insert(0, str(ROOT / "TradingAgents"))
import policies as pol

ET = ZoneInfo("America/New_York")
POLICIES_PATH = BOT_DIR / "policies.json"
TRADES_PATH = BOT_DIR / "logs" / "trades.jsonl"
MEMORY_PATH = BOT_DIR / "logs" / "decision_memory.md"
CORE_POLICY = "core_research"

# safety rails for auto-tuning
MIN_TRADES = 10          # need this many closed trades before changing a policy
MAX_WEIGHT_DELTA = 0.20  # max change to a weight per reflection cycle
WEIGHT_BOUNDS = (0.10, 2.00)


def default_policies_cfg():
    """Initial policies.json. params mirror the policy function defaults."""
    return {
        "sma200_trend": {"enabled": True, "weight": 1.0,
                         "params": {"target_frac": 0.25, "stop_pct": 0.06, "allow_inverse": False},
                         "trailing": {}, "change_log": []},
        "rsi2_meanrev": {"enabled": True, "weight": 1.0,
                         "params": {"symbol": "TQQQ", "target_frac": 0.20, "stop_pct": 0.06,
                                    "oversold": 10.0, "overbought": 70.0},
                         "trailing": {}, "change_log": []},
        "dual_momentum": {"enabled": True, "weight": 1.0,
                          "params": {"target_frac": 0.30, "stop_pct": 0.08},
                          "trailing": {}, "change_log": []},
    }


def load_policies_cfg(path=POLICIES_PATH):
    try:
        cfg = json.loads(Path(path).read_text())
    except Exception:
        cfg = {}
    # backfill any policy known to the code but missing from the file
    defaults = default_policies_cfg()
    for pid, d in defaults.items():
        cfg.setdefault(pid, d)
    return cfg


def save_policies_cfg(cfg, path=POLICIES_PATH):
    Path(path).write_text(json.dumps(cfg, indent=2))


# ---- pure: attribution -------------------------------------------------

def load_trades(path=TRADES_PATH):
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def compute_attribution(trades):
    """Per-policy realized-P&L stats from the trade log. Only closing trades
    that carry a realized `pnl` count toward win/loss; buys count deployment."""
    stats = {}
    for t in trades:
        pid = t.get("policy_id")
        if not pid:
            continue
        s = stats.setdefault(pid, {"n_buys": 0, "n_sells": 0, "n_closed": 0,
                                   "wins": 0, "losses": 0, "total_pnl": 0.0})
        if t.get("side") == "buy":
            s["n_buys"] += 1
        elif t.get("side") == "sell":
            s["n_sells"] += 1
            pnl = t.get("pnl")
            if pnl is not None:
                s["n_closed"] += 1
                s["total_pnl"] += float(pnl)
                if float(pnl) > 0:
                    s["wins"] += 1
                else:
                    s["losses"] += 1
    for pid, s in stats.items():
        s["win_rate"] = (s["wins"] / s["n_closed"]) if s["n_closed"] else None
        s["avg_pnl"] = (s["total_pnl"] / s["n_closed"]) if s["n_closed"] else None
    return stats


# ---- pure: core round-trip outcomes (for TA Reflector + memory) --------

def core_outcomes(trades):
    """Pair core-research buys with their closing sell to produce per-ticker
    outcomes for the TradingAgents Reflector. Returns a list of dicts:
    {ticker, entry_date, exit_date, dollar_in, pnl, raw_return, holding_days}.

    A round trip = the accumulated core buys for a ticker up to the sell that
    realizes a pnl. Pure: derives everything from the trade log."""
    open_lots = {}   # ticker -> {dollar_in, entry_date}
    out = []
    for t in trades:
        if t.get("policy_id") != CORE_POLICY:
            continue
        sym = t.get("symbol")
        ts = (t.get("ts") or "")[:10]
        if t.get("side") == "buy":
            lot = open_lots.setdefault(sym, {"dollar_in": 0.0, "entry_date": ts})
            lot["dollar_in"] += float(t.get("dollar_amount") or 0)
        elif t.get("side") == "sell":
            lot = open_lots.pop(sym, None)
            pnl = t.get("pnl")
            if not lot or pnl is None or not lot["dollar_in"]:
                continue
            raw = float(pnl) / lot["dollar_in"]
            hold = _days_between(lot["entry_date"], ts)
            out.append({"ticker": sym, "entry_date": lot["entry_date"], "exit_date": ts,
                        "dollar_in": round(lot["dollar_in"], 2), "pnl": float(pnl),
                        "raw_return": round(raw, 4), "holding_days": hold})
    return out


def _days_between(d0, d1):
    from datetime import date
    try:
        return max(0, (date.fromisoformat(d1) - date.fromisoformat(d0)).days)
    except (ValueError, TypeError):
        return 0


def _spy_alpha(raw_return, entry_date, exit_date):
    """Best-effort alpha vs SPY over the holding window. Falls back to the raw
    return (alpha unknown) if yfinance is unavailable."""
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(start=entry_date, end=exit_date or None)["Close"].dropna()
        if len(h) >= 2:
            spy_ret = float(h.iloc[-1]) / float(h.iloc[0]) - 1.0
            return raw_return - spy_ret
    except Exception:
        pass
    return raw_return


def reflect_core(trades, *, stamp, apply=False):
    """For each closed core round-trip, generate a prose lesson via the
    TradingAgents Reflector and attach it (with the outcome) to the decision
    memory. Best-effort and guarded — returns the number of reflections
    written. No-op if TradingAgents/the LLM/the memory log are unavailable."""
    outcomes = core_outcomes(trades)
    if not outcomes:
        return 0
    try:
        from tradingagents.graph.reflection import Reflector
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from langchain_anthropic import ChatAnthropic
    except Exception:
        return 0
    mem = TradingMemoryLog({"memory_log_path": str(MEMORY_PATH), "memory_log_max_entries": 500})
    entries = {(e["date"], e["ticker"]): e for e in mem.load_entries() if e.get("pending")}
    if not entries:
        return 0
    # AUTO-TRADER component (Anthropic; model env-overridable via BRAIN_MODEL).
    # The advisory desk never calls this and needs no Anthropic key.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    llm = ChatAnthropic(model=os.environ.get("BRAIN_MODEL", "claude-sonnet-4-6"),
                        max_tokens=400)
    reflector = Reflector(llm)
    written = 0
    for oc in outcomes:
        e = entries.get((oc["entry_date"], oc["ticker"]))
        if not e:
            continue
        alpha = _spy_alpha(oc["raw_return"], oc["entry_date"], oc["exit_date"])
        try:
            reflection = reflector.reflect_on_final_decision(
                e["decision"], oc["raw_return"], alpha, "SPY")
        except Exception:
            continue
        if apply:
            mem.update_with_outcome(oc["ticker"], oc["entry_date"], oc["raw_return"],
                                    alpha, oc["holding_days"], reflection)
        written += 1
        print(f"[reflect {stamp}] core lesson {oc['ticker']} "
              f"({oc['raw_return']:+.1%}, alpha {alpha:+.1%}): {reflection[:120]}")
    return written


# ---- pure: proposed updates (safety-railed) ----------------------------

def propose_updates(stats, policies_cfg, *, min_trades=MIN_TRADES,
                    max_delta=MAX_WEIGHT_DELTA):
    """Propose bounded weight nudges from trailing performance. Returns
    {policy_id: {old_weight, new_weight, reason}}. Does NOT mutate cfg.

    A policy needs >= min_trades closed trades to move at all; the weight
    change is capped at +-max_delta per cycle and bounded to WEIGHT_BOUNDS."""
    lo, hi = WEIGHT_BOUNDS
    proposals = {}
    for pid, cfg in policies_cfg.items():
        s = stats.get(pid)
        if not s or s.get("n_closed", 0) < min_trades:
            continue
        old = float(cfg.get("weight", 1.0))
        wr, avg = s.get("win_rate") or 0.0, s.get("avg_pnl") or 0.0
        # score in [-1, 1]: good win-rate AND positive expectancy -> up
        if avg > 0 and wr >= 0.55:
            delta = max_delta
            reason = f"win_rate {wr:.0%}, avg_pnl {avg:+.2f} -> raise"
        elif avg < 0 or wr < 0.40:
            delta = -max_delta
            reason = f"win_rate {wr:.0%}, avg_pnl {avg:+.2f} -> cut"
        else:
            continue
        new = round(max(lo, min(hi, old + delta)), 3)
        if new != old:
            proposals[pid] = {"old_weight": old, "new_weight": new, "reason": reason}
    return proposals


def apply_updates(policies_cfg, proposals, stamp):
    """Apply proposals to a cfg copy, appending change_log entries. Pure
    w.r.t. the filesystem; returns the new cfg."""
    cfg = json.loads(json.dumps(policies_cfg))  # deep copy
    for pid, p in proposals.items():
        c = cfg[pid]
        c["weight"] = p["new_weight"]
        c.setdefault("change_log", []).append({
            "ts": stamp, "old_weight": p["old_weight"],
            "new_weight": p["new_weight"], "reason": p["reason"],
        })
    return cfg


# ---- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="apply proposed updates to policies.json (Phase 4); "
                         "default is log-only")
    ap.add_argument("--notify", action="store_true",
                    help="send a notification on every applied change")
    args = ap.parse_args()
    now = datetime.now(ET)
    stamp = now.isoformat(timespec="seconds")

    cfg = load_policies_cfg()
    if not POLICIES_PATH.exists():
        save_policies_cfg(cfg)  # materialize defaults on first run

    trades = load_trades()
    stats = compute_attribution(trades)
    proposals = propose_updates(stats, cfg)

    # record trailing stats on each policy regardless of apply mode
    for pid, s in stats.items():
        if pid in cfg:
            cfg[pid]["trailing"] = {**s, "as_of": stamp}

    print(f"[reflect {stamp}] attribution: {json.dumps(stats, default=str)}")
    print(f"[reflect {stamp}] proposals: {json.dumps(proposals)}")

    # TradingAgents Reflector: prose lessons on closed core round-trips ->
    # decision memory (best-effort; only writes when --apply).
    n_core = reflect_core(trades, stamp=stamp, apply=args.apply)
    if n_core:
        print(f"[reflect {stamp}] core reflections: {n_core}")

    if args.apply and proposals:
        cfg = apply_updates(cfg, proposals, stamp)
        if args.notify:
            import subprocess
            detail = "\n".join(f"{pid}: {p['old_weight']}->{p['new_weight']} ({p['reason']})"
                               for pid, p in proposals.items())
            try:
                subprocess.run([sys.executable, str(BOT_DIR / "notify.py"),
                                f"Policy weights auto-tuned ({len(proposals)})"],
                               input=detail, text=True, timeout=60)
            except Exception:
                pass

    save_policies_cfg(cfg)  # persists trailing stats (and weights if --apply)
    print(f"[reflect {stamp}] wrote {POLICIES_PATH} (apply={args.apply})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
