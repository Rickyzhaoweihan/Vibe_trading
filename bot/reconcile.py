#!/usr/bin/env python3
"""Post-run reconciliation: single writer of state.json.

- Diffs execution_result.json against orders.json; any order on the account
  that the validator never authored => bot/logs/ALERT (blocks future runs).
- Updates state.json (positions/entry dates, cooldowns, daily trade count).
- Appends trades.jsonl and (re)writes the daily report.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT_DIR))
import trading_calendar as cal

ET = ZoneInfo("America/New_York")


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def atomic_write(path, obj):
    fd, tmp = tempfile.mkstemp(dir=str(Path(path).parent))
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def alert(reason):
    p = BOT_DIR / "logs" / "ALERT"
    stamp = datetime.now(ET).isoformat(timespec="seconds")
    with open(p, "a") as f:
        f.write(f"{stamp} {reason}\n")
    print(f"ALERT: {reason}")


def ensure_state_shape(state, today):
    """Backfill new schema keys on an older state.json and reset per-day
    ledgers when the ET date rolls over. reconcile.py is the only writer, so
    this is the single place the schema is normalized."""
    state.setdefault("positions", {})
    state.setdefault("cooldowns", {})
    state.setdefault("daily_trades", {})
    state.setdefault("pdt_ledger", {})            # {iso_date: n_daytrades}
    state.setdefault("policy_state", {})          # {policy_id: {...}}
    state.setdefault("account", {})               # {type, day_trade_limit, settlement, confirmed, detected_at}
    state.setdefault("sleeves", {
        "core": {"target_frac": 0.20},
        "aggressive": {"target_frac": 0.80},
    })
    # day-cumulative authored-order ledger (watchdog); reset on a new ET day
    at = state.get("authored_today")
    if not isinstance(at, dict) or at.get("date") != today.isoformat():
        state["authored_today"] = {"date": today.isoformat(), "orders": []}
    return state


def performance_line(state, snap):
    """Account % return vs SPY % return since baseline — the number the user cares about."""
    base = state.get("baseline")
    if not base or not snap:
        return None
    try:
        cash = float(snap["cash"])
        positions = snap.get("positions", [])
        quotes = snap.get("quotes", {})

        def mv(p):
            v = p.get("market_value")
            if v is not None:
                return float(v)
            q = quotes.get(p["symbol"])
            px = float(q.get("last_trade_price") or q.get("price")) if isinstance(q, dict) else float(q or 0)
            return float(p["quantity"]) * px

        equity = cash + sum(mv(p) for p in positions)
        acct_pct = (equity / float(base["equity"]) - 1) * 100
        import yfinance as yf
        spy = float(yf.Ticker("SPY").history(period="1d")["Close"].iloc[-1])
        spy_pct = (spy / float(base["spy_close"]) - 1) * 100
        alpha = acct_pct - spy_pct
        return (f"**Performance since {base['date']}**: account ${equity:.2f} "
                f"({acct_pct:+.2f}%) vs SPY {spy_pct:+.2f}% — alpha {alpha:+.2f} pp")
    except Exception as e:
        return f"(performance calc unavailable: {e})"


def next_trading_day(d):
    nxt = d + timedelta(days=1)
    while not cal.is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--claude-rc", type=int, default=0)
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    now = datetime.now(ET)
    today = now.date()

    orders_doc = load(run_dir / "orders.json")
    exec_doc = load(run_dir / "execution_result.json")
    decisions = load(run_dir / "decisions.json", {})
    state = load(BOT_DIR / "state.json", {"positions": {}, "cooldowns": {}, "daily_trades": {}})
    state = ensure_state_shape(state, today)
    paper = bool(orders_doc and orders_doc.get("paper"))
    authored = (orders_doc or {}).get("orders", [])

    # ---- anomaly checks ----
    if args.claude_rc != 0:
        alert(f"executor exited rc={args.claude_rc}")
    if orders_doc is None:
        alert("orders.json missing — guardrails never ran")
    if not paper and authored and exec_doc is None:
        alert("orders were authored but execution_result.json is missing")

    placed = (exec_doc or {}).get("placed", [])
    account_orders = (exec_doc or {}).get("account_orders_today", [])
    # Watchdog: an order on the account that NO run today authored (and that
    # isn't an existing tracked position) is unexpected -> halt. We compare
    # against the DAY-CUMULATIVE authored ledger, not just this run, because
    # an intraday daemon fires many small runs and each must tolerate the
    # orders placed by earlier runs of the same day.
    authored_today_keys = {
        (o.get("symbol"), o.get("side")) for o in state["authored_today"]["orders"]
    }
    authored_today_keys |= {(o["symbol"], o["side"]) for o in authored}
    state_pos_syms = set(state["positions"])
    for ao in account_orders:
        key = (ao.get("symbol"), ao.get("side"))
        if key not in authored_today_keys and ao.get("symbol") not in state_pos_syms:
            alert(f"unexpected order on account: {key}")

    # record this run's authored orders into the cumulative day ledger
    for o in authored:
        state["authored_today"]["orders"].append(
            {"symbol": o.get("symbol"), "side": o.get("side"), "ref_id": o.get("ref_id")}
        )

    snap = load(run_dir / "snapshot.json", {})

    def quote_px(sym):
        q = (snap.get("quotes") or {}).get(sym)
        if isinstance(q, dict):
            return float(q.get("last_trade_price") or q.get("price") or 0) or None
        return float(q) if q is not None else None

    # ---- apply fills to state (paper mode applies intended orders) ----
    BAD_STATUSES = ("rejected", "failed", "cancelled", "voided", "error")
    ok_placed = [p for p in placed if str(p.get("status", "")).lower() not in BAD_STATUSES]
    broker_rejected = [p for p in placed if p not in ok_placed]
    effective = authored if paper else [
        o for o in authored if any(p.get("order_id") == o["order_id"] for p in ok_placed)
    ]
    dec_by_sym = {d["ticker"]: d for d in decisions.get("decisions", [])}
    trades_log = open(BOT_DIR / "logs" / "trades.jsonl", "a")
    for o in effective:
        sym = o["symbol"]
        # policy-driven (intents) orders carry their own tags; legacy rating
        # orders fall back to dec_by_sym.
        is_intent = "policy_id" in o or "hold_class" in o
        if o["side"] == "buy":
            prev = state["positions"].get(sym, {})
            prev_stop = prev.get("stop_loss")
            if is_intent and o.get("stop_pct") and quote_px(sym):
                new_stop = round(quote_px(sym) * (1 - float(o["stop_pct"])), 2)
            else:
                new_stop = dec_by_sym.get(sym, {}).get("stop_loss")
            rec = {
                "entry_date": prev.get("entry_date", today.isoformat()),
                "entry_ts": prev.get("entry_ts", now.isoformat(timespec="seconds")),
                "dollar_in": float(prev.get("dollar_in", 0)) + float(o.get("dollar_amount", 0)),
                "stop_loss": max((s for s in (prev_stop, new_stop) if s is not None), default=None),
                "order_id": o["order_id"],
            }
            if is_intent:
                rec.update(
                    policy_id=o.get("policy_id"),
                    sleeve=o.get("sleeve", "aggressive"),
                    hold_class=o.get("hold_class", "swing_lev"),
                    # opened_today stays true if it was already opened today
                    opened_today=prev.get("opened_today", False) or (
                        prev.get("entry_date", today.isoformat()) == today.isoformat()),
                )
            else:
                rec["last_rating"] = dec_by_sym.get(sym, {}).get("rating")
            state["positions"][sym] = rec
            pnl = dollar_in = proceeds = None
            log_policy = rec.get("policy_id") or o.get("policy_id")
        else:
            # closing a position: realize P&L for per-policy attribution
            closed = state["positions"].get(sym, {})
            dollar_in = float(closed.get("dollar_in", 0)) or None
            px = quote_px(sym)
            proceeds = round(float(o.get("quantity", 0)) * px, 2) if px else None
            pnl = round(proceeds - dollar_in, 2) if (proceeds is not None and dollar_in) else None
            log_policy = closed.get("policy_id") or o.get("policy_id")
            # a sell that closes a position opened today is a PDT day-trade
            if o.get("closes_daytrade"):
                k = today.isoformat()
                state["pdt_ledger"][k] = state["pdt_ledger"].get(k, 0) + 1
            state["positions"].pop(sym, None)
            state["cooldowns"][sym] = next_trading_day(today).isoformat()
        trades_log.write(json.dumps({
            "ts": now.isoformat(timespec="seconds"),
            "run": run_dir.name,
            "paper": paper,
            **{k: o.get(k) for k in ("symbol", "side", "dollar_amount", "quantity", "order_id")},
            "policy_id": log_policy,
            "dollar_in": dollar_in,
            "proceeds": proceeds,
            "pnl": pnl,
            "rating": dec_by_sym.get(sym, {}).get("rating"),
        }) + "\n")
    trades_log.close()

    # ---- account-type detection (cash vs margin -> PDT/settlement budget) ----
    # Fail-closed: account_type.classify defaults to cash/zero-daytrades unless
    # a live get_accounts payload positively confirms margin.
    accounts_raw = snap.get("accounts_raw")
    if accounts_raw is not None:
        import account_type as at
        info = at.classify(accounts_raw)
        info["detected_at"] = now.isoformat(timespec="seconds")
        state["account"] = info

    # ---- trailing stop: ratchet stops up to 12% below latest price ----
    TRAIL_PCT = 0.12
    for sym, pos in state["positions"].items():
        q = (snap.get("quotes") or {}).get(sym)
        px = float(q.get("last_trade_price") or q.get("price")) if isinstance(q, dict) else (
            float(q) if q is not None else None
        )
        if px:
            trail = round(px * (1 - TRAIL_PCT), 2)
            if pos.get("stop_loss") is None or trail > float(pos["stop_loss"]):
                pos["stop_loss"] = trail

    state["daily_trades"][today.isoformat()] = (
        state["daily_trades"].get(today.isoformat(), 0) + len(effective)
    )
    state["last_run"] = {
        "slot": decisions.get("slot"),
        "ts": now.isoformat(timespec="seconds"),
        "status": "ok" if not (BOT_DIR / "logs" / "ALERT").exists() else "alert",
        "run_dir": str(run_dir),
    }
    # keep daily_trades small
    state["daily_trades"] = {
        k: v for k, v in state["daily_trades"].items()
        if (today - datetime.fromisoformat(k).date()).days <= 7
    }
    atomic_write(BOT_DIR / "state.json", state)

    # ---- daily report ----
    report = BOT_DIR / "reports" / f"{today.isoformat()}.md"
    report.parent.mkdir(exist_ok=True)
    lines = [] if not report.exists() else [report.read_text()]
    mode = "PAPER" if paper else "LIVE"
    lines.append(f"\n## Run {run_dir.name} ({decisions.get('slot', '?')}, {mode})\n")
    lines.append("| Ticker | Rating | Action |\n|---|---|---|\n")
    acted = {o["symbol"]: o["side"] for o in effective}
    rej = {r[0]: r[1] for r in (orders_doc or {}).get("rejections", [])}
    for p in broker_rejected:
        summary = str(p.get("response_summary", ""))[:160]
        rej[p.get("symbol")] = f"BROKER REJECTED: {summary}"
    if broker_rejected:
        import subprocess
        detail = "\n\n".join(
            f"{p.get('symbol')} {p.get('side')}: {p.get('response_summary', '')}"
            for p in broker_rejected
        )
        subprocess.run(
            [sys.executable, str(BOT_DIR / "notify.py"),
             f"Order(s) rejected by Robinhood ({len(broker_rejected)})"],
            input=detail, text=True, timeout=60,
        )
    for d in decisions.get("decisions", []):
        t = d["ticker"]
        action = acted.get(t, rej.get(t, "no action"))
        if d.get("error"):
            action = f"analysis error: {d['error'][:60]}"
        lines.append(f"| {t} | {d.get('rating')} | {action} |\n")
    perf = performance_line(state, snap)
    if perf:
        lines.append(f"\n{perf}\n")
    if (BOT_DIR / "logs" / "ALERT").exists():
        lines.append("\n**ALERT file present — trading halted until cleared.**\n")
    report.write_text("".join(lines))
    print(f"reconciled: {len(effective)} effective orders, report {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
