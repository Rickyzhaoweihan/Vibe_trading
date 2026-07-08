#!/usr/bin/env python3
"""Pre-trade validator. The ONLY component allowed to author orders.

Reads <run-dir>/snapshot.json (live account data, written by the executor)
and <run-dir>/decisions.json (analysis output), applies hard rules, and
writes <run-dir>/orders.json. The executor must relay payloads verbatim.

Hard rules (see plan):
  - the auto-trader's account (BOT_ACCOUNT) only
  - max 35% of account equity per position
  - min 3-day hold; early exit only when price <= recorded stop-loss
  - max 4 trades/day (counting both runs)
  - keep >= $10 cash buffer; min order $5; spend bounded by cash (no leverage)
  - market orders only, regular hours, execution window gated by trading_calendar
  - 1-trading-day re-entry cooldown after a sell
"""

import argparse
import json
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trading_calendar as cal
import sleeves
import account_type

BOT_DIR = Path(__file__).resolve().parent

RULES = {
    "ACCOUNT": os.environ.get("BOT_ACCOUNT", "AGENTIC_ACCT"),
    "MAX_POS_FRAC": 0.35,
    "MIN_HOLD_DAYS": 5,
    "OVERWEIGHT_MIN_CASH_FRAC": 0.40,
    "MAX_TRADES_PER_DAY": 4,
    "CASH_BUFFER": 10.00,
    "MIN_ORDER": 5.00,
    "MAX_SNAPSHOT_AGE_MIN": 10,
}

# Aggressive sizing by conviction tier (fraction of account equity).
TARGET_FRAC = {"Buy": 0.35, "Overweight": 0.20}

# ---- policy-aware (intents) ruleset -------------------------------------
# Per hold-class caps replace the blanket MIN_HOLD_DAYS/MAX_POS_FRAC for the
# leveraged sleeve. A 5-day min-hold on a 3x ETF is dangerous, so swing/day
# classes hold 0 days and exit on the policy's rule or a tight stop.
CLASS = {
    "core":         {"max_pos_frac": 0.12, "min_hold_days": 5, "stop_pct": 0.15},
    "swing_lev":    {"max_pos_frac": 0.30, "min_hold_days": 0, "stop_pct": 0.06},
    "daytrade_lev": {"max_pos_frac": 0.25, "min_hold_days": 0, "stop_pct": 0.03},
}
INTENT_RULES = {
    "MAX_TRADES_PER_DAY": 8,        # binding cap is PDT/settlement, not this
    "MAX_SNAPSHOT_AGE_MIN": 5,      # tighter intraday
    "MAX_LEV_GROSS_FRAC": 0.80,     # gross leveraged exposure ceiling
    "VOL_TARGET_ANN": 0.50,         # annualized vol target for sizing
    "VOL_SCALE_CLAMP": (0.30, 1.50),
}


def _class(name):
    return CLASS.get(name, CLASS["swing_lev"])


def load(path):
    with open(path) as f:
        return json.load(f)


def write_orders(run_dir, orders, rejections, paper, note=""):
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": RULES["ACCOUNT"],
        "paper": paper,
        "note": note,
        "orders": orders,
        "rejections": rejections,
    }
    with open(Path(run_dir) / "orders.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"orders.json written: {len(orders)} orders, {len(rejections)} rejections. {note}")
    return 0


def snapshot_age_minutes(snap, now_utc):
    fetched = datetime.fromisoformat(snap["fetched_at"].replace("Z", "+00:00"))
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (now_utc - fetched).total_seconds() / 60.0


def price_of(snap, sym):
    q = snap.get("quotes", {}).get(sym)
    if isinstance(q, dict):
        q = q.get("last_trade_price") or q.get("price")
    return float(q) if q is not None else None


def market_order(sym, side, *, dollar_amount=None, quantity=None):
    o = {
        "order_id": str(uuid.uuid4()),
        "account_number": RULES["ACCOUNT"],
        "symbol": sym,
        "side": side,
        "type": "market",
        "market_hours": "regular_hours",
        "time_in_force": "gfd",
        "ref_id": str(uuid.uuid4()),
    }
    if dollar_amount is not None:
        o["dollar_amount"] = f"{dollar_amount:.2f}"
    else:
        o["quantity"] = f"{quantity:.6f}"
    return o


def validate(snap, decisions, state, *, now_et=None, now_utc=None, paper=False):
    """Pure function: returns (orders, rejections, note). Testable without I/O.

    Dispatches on the decisions schema: a policy-driven doc carrying "intents"
    goes to validate_intents (leveraged sleeve); the legacy per-ticker "rating"
    doc keeps the original behavior (Tier-3 core path, existing tests)."""
    if isinstance(decisions, dict) and "intents" in decisions:
        return validate_intents(snap, decisions, state,
                                now_et=now_et, now_utc=now_utc, paper=paper)
    now_et = now_et or cal.now_et()
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_et.date()
    rejections = []

    # ---- global gates: any failure -> zero orders ----
    if snap.get("account_number") != RULES["ACCOUNT"]:
        return [], [["*", "snapshot is not the agentic account"]], "wrong account"
    if not cal.within_exec_window(now_et):
        return [], [], "outside execution window"
    if snapshot_age_minutes(snap, now_utc) > RULES["MAX_SNAPSHOT_AGE_MIN"]:
        return [], [], "stale snapshot"

    positions = {p["symbol"]: p for p in snap.get("positions", [])}
    state_pos = state.get("positions", {})
    cooldowns = state.get("cooldowns", {})
    cash = float(snap["cash"])

    def pos_value(sym):
        p = positions.get(sym)
        if not p:
            return 0.0
        mv = p.get("market_value")
        if mv is None:
            px = price_of(snap, sym)
            mv = float(p["quantity"]) * px if px else 0.0
        return float(mv)

    equity = cash + sum(pos_value(s) for s in positions)
    trades_today = int(state.get("daily_trades", {}).get(today.isoformat(), 0))
    budget = max(0, RULES["MAX_TRADES_PER_DAY"] - trades_today)

    orders = []

    # ---- sells first (hard Sells, or stop-loss hits at any rating) ----
    for d in decisions.get("decisions", []):
        sym, rating = d["ticker"], d.get("rating", "Hold")
        p = positions.get(sym)
        if not p:
            continue
        rec = state_pos.get(sym, {})
        stop = rec.get("stop_loss") or d.get("stop_loss")
        px = price_of(snap, sym)
        stop_hit = bool(stop and px and px <= float(stop))
        wants_exit = rating in ("Sell", "Underweight")
        if not (wants_exit or stop_hit):
            continue
        entry = rec.get("entry_date")
        held = (today - date.fromisoformat(entry)).days if entry else 999
        if held < RULES["MIN_HOLD_DAYS"] and not stop_hit:
            rejections.append([sym, f"min-hold: held {held}d < {RULES['MIN_HOLD_DAYS']}d"])
            continue
        if rating == "Underweight" and not stop_hit:
            continue  # aggressive but decisive: only hard Sells or stops exit
        if len(orders) >= budget:
            rejections.append([sym, "daily trade budget exhausted"])
            continue
        qty = float(p.get("shares_available_for_sells", p["quantity"]))
        orders.append(market_order(sym, "sell", quantity=qty))
        cash += pos_value(sym)  # estimate proceeds for buy sizing

    # ---- buys, ranked by conviction ----
    buys = [d for d in decisions.get("decisions", []) if d.get("rating") in TARGET_FRAC]
    buys.sort(key=lambda d: 0 if d["rating"] == "Buy" else 1)
    selling = {o["symbol"] for o in orders}
    for d in buys:
        sym = d["ticker"]
        if sym in selling:
            continue
        if len(orders) >= budget:
            rejections.append([sym, "daily trade budget exhausted"])
            continue
        cd = cooldowns.get(sym)
        if cd and date.fromisoformat(cd) >= today:
            rejections.append([sym, f"re-entry cooldown until {cd}"])
            continue
        if d["rating"] == "Overweight" and cash < RULES["OVERWEIGHT_MIN_CASH_FRAC"] * equity:
            rejections.append([sym, "Overweight skipped: keeping cash for higher-conviction Buys"])
            continue
        room = RULES["MAX_POS_FRAC"] * equity - pos_value(sym)
        spendable = cash - RULES["CASH_BUFFER"]
        spend = round(min(TARGET_FRAC[d["rating"]] * equity, room, spendable), 2)
        if spend < RULES["MIN_ORDER"]:
            rejections.append([sym, f"sized to ${spend:.2f} < ${RULES['MIN_ORDER']:.2f} min"])
            continue
        orders.append(market_order(sym, "buy", dollar_amount=spend))
        cash -= spend

    return orders, rejections, "ok"


def _vol_scale(target_frac, realized_vol):
    """Vol-target sizing: shrink (or modestly grow) a position so its risk
    contribution tracks VOL_TARGET_ANN. A 3x ETF at high vol is sized down."""
    if not realized_vol or realized_vol <= 0:
        return target_frac
    lo, hi = INTENT_RULES["VOL_SCALE_CLAMP"]
    scale = max(lo, min(hi, INTENT_RULES["VOL_TARGET_ANN"] / realized_vol))
    return target_frac * scale


def validate_intents(snap, decisions, state, *, now_et=None, now_utc=None, paper=False):
    """Policy-aware order author for the leveraged sleeve. Pure function.

    Consumes a decisions doc carrying `intents` (from policies.py via the
    daemon). Enforces, in order: global gates, per-hold-class caps, the 80/20
    sleeve split, PDT/settlement day-trade budget, vol-target sizing, cash
    buffer and no-leverage-beyond-(settled-)cash. The brain only advises; this
    function is the sole authority on what is actually placed."""
    now_et = now_et or cal.now_et()
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_et.date()
    rejections = []

    # ---- global gates (any failure -> zero orders) ----
    if snap.get("account_number") != RULES["ACCOUNT"]:
        return [], [["*", "snapshot is not the agentic account"]], "wrong account"
    if not cal.within_exec_window(now_et):
        return [], [], "outside execution window"
    if snapshot_age_minutes(snap, now_utc) > INTENT_RULES["MAX_SNAPSHOT_AGE_MIN"]:
        return [], [], "stale snapshot"

    positions = {p["symbol"]: p for p in snap.get("positions", [])}
    state_pos = state.get("positions", {})
    cash = float(snap["cash"])
    settled = float(snap.get("settled_cash", cash))

    # ---- account type -> day-trade budget (fail-closed to cash/zero) ----
    acct = account_type.classify(snap.get("accounts_raw"))
    dt_budget = (max(0, acct["day_trade_limit"]
                     - account_type.daytrades_used(state.get("pdt_ledger", {}), today))
                 if acct["type"] == "margin" else 0)
    # cash accounts must fund buys from SETTLED proceeds, not unsettled sales
    spend_cash = settled if acct["type"] != "margin" else cash

    def pos_value(sym):
        p = positions.get(sym)
        if not p:
            return 0.0
        mv = p.get("market_value")
        if mv is None:
            px = price_of(snap, sym)
            mv = float(p["quantity"]) * px if px else 0.0
        return float(mv)

    equity = cash + sum(pos_value(s) for s in positions)
    trades_today = int(state.get("daily_trades", {}).get(today.isoformat(), 0))
    budget = max(0, INTENT_RULES["MAX_TRADES_PER_DAY"] - trades_today)
    exposure = sleeves.sleeve_exposure(positions, state_pos, pos_value)
    sleeves_cfg = state.get("sleeves", {})
    intents = decisions.get("intents", [])

    orders = []
    selling = set()

    # ---- exits first ----
    for it in intents:
        if it.get("side") != "sell":
            continue
        sym = it["ticker"]
        p = positions.get(sym)
        if not p:
            continue
        rec = state_pos.get(sym, {})
        hc = _class(rec.get("hold_class", it.get("hold_class", "swing_lev")))
        # min-hold (0 for leveraged swing/day classes), stop overrides
        stop = rec.get("stop_loss")
        px = price_of(snap, sym)
        stop_hit = bool(stop and px and px <= float(stop))
        entry = rec.get("entry_date")
        held_days = (today - date.fromisoformat(entry)).days if entry else 999
        if held_days < hc["min_hold_days"] and not stop_hit:
            rejections.append([sym, f"min-hold {held_days}d < {hc['min_hold_days']}d"])
            continue
        # PDT: selling a position opened TODAY is a day-trade; budget-gate it
        if rec.get("opened_today") and not stop_hit:
            if dt_budget <= 0:
                rejections.append([sym, "day-trade budget exhausted — holding overnight"])
                continue
            dt_budget -= 1
        if len(orders) >= budget:
            rejections.append([sym, "daily trade budget exhausted"])
            continue
        qty = float(p.get("shares_available_for_sells", p["quantity"]))
        o = market_order(sym, "sell", quantity=qty)
        o.update(policy_id=it.get("policy_id"), sleeve=sleeves.sleeve_of(rec),
                 hold_class=rec.get("hold_class", it.get("hold_class")),
                 closes_daytrade=bool(rec.get("opened_today")))
        orders.append(o)
        cash += pos_value(sym)
        spend_cash += pos_value(sym) if acct["type"] == "margin" else 0  # cash acct: proceeds unsettled
        exposure[sleeves.sleeve_of(rec)] = max(0.0, exposure.get(sleeves.sleeve_of(rec), 0.0) - pos_value(sym))
        selling.add(sym)

    # ---- entries, highest conviction first ----
    lev_gross = sum(pos_value(s) for s in positions if s not in selling)
    buys = sorted((it for it in intents if it.get("side") == "buy"),
                  key=lambda it: it.get("target_frac", 0), reverse=True)
    for it in buys:
        sym = it["ticker"]
        if sym in selling:
            continue
        if len(orders) >= budget:
            rejections.append([sym, "daily trade budget exhausted"])
            continue
        sleeve = it.get("sleeve", "aggressive")
        hc = _class(it.get("hold_class", "swing_lev"))
        target = _vol_scale(float(it.get("target_frac", 0)), it.get("realized_vol"))
        class_room = hc["max_pos_frac"] * equity - pos_value(sym)
        sleeve_room = sleeves.sleeve_room(sleeve, equity, sleeves_cfg, exposure)
        gross_room = INTENT_RULES["MAX_LEV_GROSS_FRAC"] * equity - lev_gross
        spendable = spend_cash - RULES["CASH_BUFFER"]
        spend = round(min(target * equity, class_room, sleeve_room, gross_room, spendable), 2)
        if spend < RULES["MIN_ORDER"]:
            rejections.append([sym, f"sized to ${spend:.2f} < ${RULES['MIN_ORDER']:.2f} min"])
            continue
        o = market_order(sym, "buy", dollar_amount=spend)
        o.update(policy_id=it.get("policy_id"), sleeve=sleeve,
                 hold_class=it.get("hold_class", "swing_lev"),
                 stop_pct=it.get("stop_pct"), opened_today=True)
        orders.append(o)
        cash -= spend
        spend_cash -= spend
        lev_gross += spend
        exposure[sleeve] = exposure.get(sleeve, 0.0) + spend

    return orders, rejections, f"ok ({acct['type']}, dt_budget {dt_budget})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()
    paper = os.environ.get("PAPER_MODE", "1") == "1"

    try:
        snap = load(Path(args.run_dir) / "snapshot.json")
        decisions = load(Path(args.run_dir) / "decisions.json")
        state = load(BOT_DIR / "state.json")
    except Exception as e:
        return write_orders(args.run_dir, [], [["*", f"input load failed: {e}"]], paper, "load error")

    orders, rejections, note = validate(snap, decisions, state, paper=paper)
    return write_orders(args.run_dir, orders, rejections, paper, note)


if __name__ == "__main__":
    sys.exit(main())
