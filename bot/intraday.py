#!/usr/bin/env python3
"""The intraday daemon — Tiers 0/1/2 in a live loop.

Launched at the open, self-terminates near the close. Each tick it recomputes
the regime (Tier 0, $0) and the active policies' intents (Tier 1, $0), and
re-routes via the brain (Tier 2, one cheap LLM call) only on an interval or a
regime change. It spends tokens on the execution relay ONLY when a tick
actually produces orders — most ticks are free.

Trust boundary unchanged: when there are intents, the daemon writes
decisions.json and invokes the SAME relay run.sh uses. The relay runs
guardrails.py (the sole order author) and places only what guardrails wrote;
reconcile.py (the sole state writer) then reconciles. The daemon never places
an order or writes state.json itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))

import trading_calendar as cal
import regime as rg
import policies as pol
import brain
import reflect
import signals
import deepresearch

ET = ZoneInfo("America/New_York")
PY = str(ROOT / ".venv" / "bin" / "python")

# symbols we need daily bars for: indices for the regime + the tradable set
INDEX_SYMS = ["QQQ", "SPY"]
UNIVERSE = INDEX_SYMS + sorted(pol.ALL_LEV)

TICK_SECONDS = int(os.environ.get("DAEMON_TICK", "90"))
BRAIN_INTERVAL_SEC = int(os.environ.get("DAEMON_BRAIN_INTERVAL", str(75 * 60)))
CLOSE_BUFFER_SEC = int(os.environ.get("DAEMON_CLOSE_BUFFER", str(10 * 60)))
HISTORY_REFRESH_SEC = int(os.environ.get("DAEMON_HISTORY_REFRESH", str(15 * 60)))
# Deep-confirm gate: run a TradingAgents check on the underlying index before
# any leveraged BUY, vetoing entries the multi-agent stack reads as bearish.
CONFIRM_ENTRIES = os.environ.get("CONFIRM_ENTRIES", "1") == "1"

# relay tool allowlists (mirror run.sh)
RO_TOOLS = ("mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,"
            "mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_equity_quotes,"
            "mcp__robinhood-trading__get_equity_orders,Read,Write,"
            "Bash(python3 bot/*),Bash(.venv/bin/python bot/*)")
TRADE_TOOLS = RO_TOOLS + ",mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__place_equity_order"


def log(msg):
    line = f"[intraday {datetime.now(ET).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(BOT_DIR / "logs" / "bot.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        return json.loads((BOT_DIR / "state.json").read_text())
    except Exception:
        return {"positions": {}}


def acquire_lock():
    lock = BOT_DIR / ".daemon.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        return None
    (lock / "pid").write_text(str(os.getpid()))
    return lock


def alert_present():
    return (BOT_DIR / "logs" / "ALERT").exists()


def build_market():
    market = rg.fetch_market(UNIVERSE)
    vix = rg.fetch_vix()
    return market, vix


def evaluate_tick(market, vix, policies_cfg, routing):
    """Pure-ish: regime + intents for this tick (no orders placed)."""
    regime_out = rg.compute_regime(market, index="QQQ", vix=vix)
    state = load_state()
    positions = state.get("positions", {})
    intents = pol.evaluate(routing, regime_out, market, positions, policies_cfg)
    return regime_out, positions, intents


def run_relay_and_reconcile(run_dir, paper):
    """Invoke the same relay run.sh uses, then reconcile. Returns the relay rc."""
    prompt_file = BOT_DIR / "prompts" / ("execute_paper.md" if paper else "execute_trades.md")
    tools = RO_TOOLS if paper else TRADE_TOOLS
    prompt = prompt_file.read_text().replace("{{RUN_DIR}}", str(run_dir))
    ts = run_dir.name.replace("intraday_", "")
    exec_log = BOT_DIR / "logs" / f"exec_{ts}.json"
    with open(exec_log, "w") as out:
        rc = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", tools, "--output-format", "json"],
            stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT),
        ).returncode
    log(f"relay finished rc={rc} ({'paper' if paper else 'live'})")
    subprocess.run([PY, str(BOT_DIR / "reconcile.py"), "--run-dir", str(run_dir),
                    "--claude-rc", str(rc)], cwd=str(ROOT))
    return rc


def memory_context():
    """Cross-ticker lessons from the TradingAgents decision memory (best-effort),
    injected into routing so the brain learns from past core-research outcomes."""
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        mem = TradingMemoryLog({"memory_log_path": str(BOT_DIR / "logs" / "decision_memory.md")})
        return mem.get_past_context("QQQ", n_same=2, n_cross=3)
    except Exception:
        return ""


def confirm_buys(intents, date):
    """Deep-confirm gate: drop leveraged BUY intents the TradingAgents stack
    reads as bearish on the underlying index. Sells always pass. Fail-open."""
    if not CONFIRM_ENTRIES:
        return intents
    kept = []
    for it in intents:
        if it.get("side") != "buy":
            kept.append(it)
            continue
        allow, info = deepresearch.confirm_entry(it["ticker"], date)
        if allow:
            kept.append(it)
            log(f"confirm OK {it['ticker']} (underlying {deepresearch.underlying_of(it['ticker'])} "
                f"= {info.get('rating')})")
        else:
            log(f"VETO buy {it['ticker']}: underlying "
                f"{deepresearch.underlying_of(it['ticker'])} rated {info.get('rating')}")
    return kept


def trade_cycle(regime_out, routing, intents, paper):
    if (BOT_DIR / ".lock").exists():
        log("off-hours run holds bot/.lock; skipping trade cycle this tick")
        return
    ts = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    run_dir = BOT_DIR / "runs" / f"intraday_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "slot": "intraday",
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "regime": regime_out,
        "routing": routing,
        "intents": intents,
    }
    (run_dir / "decisions.json").write_text(json.dumps(doc, indent=2))
    log(f"intents -> {run_dir.name}: " +
        ", ".join(f"{i['side']} {i['ticker']}" for i in intents))
    run_relay_and_reconcile(run_dir, paper)


def loop(paper, *, once=False, dry_run=False):
    policies_cfg = reflect.load_policies_cfg()
    market, vix = build_market()
    last_history = time.time()
    routing = None
    last_brain = 0.0
    last_label = None

    while True:
        now = cal.now_et()
        if not dry_run and not once:
            if not cal.is_open_now(now) or cal.seconds_to_close(now) <= CLOSE_BUFFER_SEC:
                log("near/after close — daemon exiting")
                break
        if alert_present():
            log("ALERT present — halting daemon")
            break

        if time.time() - last_history > HISTORY_REFRESH_SEC:
            market, vix = build_market()
            last_history = time.time()

        # Tier 0
        regime_out = rg.compute_regime(market, index="QQQ", vix=vix)
        label = regime_out["label"]

        # Tier 2 — route on interval, on regime change, or first pass
        need_route = (routing is None or label != last_label
                      or time.time() - last_brain > BRAIN_INTERVAL_SEC)
        if need_route:
            if dry_run:
                routing = brain.default_route(regime_out, policies_cfg)
            else:
                state = load_state()
                perf = {pid: c.get("trailing", {}) for pid, c in policies_cfg.items()}
                today_str = now.strftime("%Y-%m-%d")
                news = signals.routing_context(today_str)        # TA news/sentiment
                mem = memory_context()                           # TA decision memory
                routing = brain.route(regime_out, perf, _dt_budget(state),
                                      state.get("positions", {}), news=news, memory=mem)
            last_brain = time.time()
            last_label = label
            log(f"regime={label} routing={routing.get('active_policies')} "
                f"aggr={routing.get('aggressiveness')} src={routing.get('source')}")

        # Tier 1
        state = load_state()
        intents = pol.evaluate(routing, regime_out, market,
                               state.get("positions", {}), policies_cfg)

        if intents and not dry_run:
            intents = confirm_buys(intents, now.strftime("%Y-%m-%d"))  # deep-confirm gate
            if intents:
                trade_cycle(regime_out, routing, intents, paper)
            else:
                log("all intents vetoed by deep-confirm gate")
        elif intents:
            log("[dry-run] intents: " +
                ", ".join(f"{i['side']} {i['ticker']} tf={i['target_frac']}" for i in intents))
        else:
            log(f"tick: regime={label}, no intents")

        if once or dry_run:
            break
        time.sleep(TICK_SECONDS)


def _dt_budget(state):
    import account_type as at
    from datetime import date
    acct = state.get("account", {})
    if acct.get("type") != "margin":
        return 0
    used = at.daytrades_used(state.get("pdt_ledger", {}), date.today())
    return max(0, acct.get("day_trade_limit", 0) - used)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute regime/routing/intents and log them; place nothing, "
                         "no LLM brain call (uses deterministic routing)")
    ap.add_argument("--paper", dest="paper", action="store_true", default=None)
    ap.add_argument("--live", dest="paper", action="store_false")
    args = ap.parse_args()

    paper = (os.environ.get("PAPER_MODE", "1") == "1") if args.paper is None else args.paper

    if args.dry_run:
        log("DRY RUN — no lock, no orders")
        loop(paper=True, dry_run=True)
        return 0

    lock = acquire_lock()
    if lock is None:
        log("another daemon holds the lock; exiting")
        return 0
    if alert_present():
        log("ALERT present at startup; exiting")
        return 0
    try:
        log(f"daemon starting (paper={paper}, tick={TICK_SECONDS}s)")
        loop(paper=paper, once=args.once)
    finally:
        import shutil
        shutil.rmtree(lock, ignore_errors=True)
        log("daemon stopped, lock released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
