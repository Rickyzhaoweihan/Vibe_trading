#!/usr/bin/env python3
"""Intraday monitor — high-conviction alerts only.

A cheap loop during regular hours. Each tick it recomputes a few triggers from
free price data and, on a NEW high-conviction trigger, sends one iMessage. It
dedupes per (date, kind, ticker) so it never spams the same alert. It self-
terminates near the close.

Triggers:
  - per-name big move vs prior close
  - macro-regime shift (10Y bp jump, VIX stress)
  - momentum-UNWIND (the L2 score crosses 'high')
  - big BTC move

ADVISORY ONLY — no order tools; it only messages. An optional read-only relay
(--llm) can sharpen the alert text.

  python bot/desk/monitor.py --once --dry      # one tick, print triggers, send nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import regime as rg
import macro as L1
import sectors as L2
import synthesize as L6
import health as H
import snapshot as SNAP
import trading_calendar as cal

ET = ZoneInfo("America/New_York")

# How often (in ticks) to re-snapshot the live account to catch trades the user
# placed during the session. 0 disables intraday account-watching. Default: every
# 3 ticks (~30 min at the 600s cadence).
SNAP_EVERY = int(os.environ.get("DESK_SNAP_EVERY", "3"))
_ACTIVITY_VERB_ZH = {"NEW": "新建", "CLOSED": "清仓", "ADDED": "加仓", "REDUCED": "减仓"}


# ---- pure trigger logic -------------------------------------------------

def pct_move(last, prev_close):
    if last is None or not prev_close:
        return None
    return last / prev_close - 1.0


def name_triggers(sym, ind, cfg=None):
    """Per-name triggers from a symbol's indicator bundle."""
    cfg = cfg or conf.ALERT
    fired = []
    mv = pct_move(ind.get("last"), ind.get("prev_close"))
    if mv is not None and abs(mv) >= cfg["move_pct"]:
        fired.append({"kind": "move", "ticker": sym,
                      "detail": f"⚡ {sym} 较昨收 {mv:+.1%}（现价 ${ind.get('last'):,.2f}）"})
    return fired


def macro_triggers(market, vix, cfg=None):
    """Book-wide macro triggers: 10Y yield jump, VIX stress, BTC move."""
    cfg = cfg or conf.ALERT
    fired = []
    tnx = rg.indicators(market.get(conf.MACRO_PANEL["rates_10y"], {}))
    if tnx.get("last") is not None and tnx.get("prev_close"):
        bp = (tnx["last"] - tnx["prev_close"]) * 100.0
        if abs(bp) >= cfg["tnx_bp_jump"]:
            fired.append({"kind": "rates", "ticker": "^TNX",
                          "detail": f"📉 10年期收益率 {bp:+.0f}bp — 压制成长/半导体"})
    if vix is not None and vix >= cfg["vix_stress"]:
        fired.append({"kind": "vix", "ticker": "^VIX",
                      "detail": f"⚠️ VIX {vix:.0f} — 风险情绪转紧"})
    btc = rg.indicators(market.get(conf.MACRO_PANEL["btc"], {}))
    mv = pct_move(btc.get("last"), btc.get("prev_close"))
    if mv is not None and abs(mv) >= cfg["btc_move_pct"]:
        fired.append({"kind": "btc", "ticker": "BTC", "detail": f"₿ BTC {mv:+.1%}"})
    return fired


def unwind_trigger(unwind):
    if unwind.get("band") == "high":
        return [{"kind": "unwind", "ticker": "BOOK",
                 "detail": f"🔻 动量瓦解风险 高({unwind.get('score')}) — 给拥挤持仓降风险："
                           + "；".join(unwind.get("reasons", [])[:2])}]
    return []


# ---- leveraged-hedge tight watch (SQQQ -3x etc.) ------------------------

def held_leveraged_hedges(holdings):
    """Held symbols that are leveraged inverse ETFs (decay + move ~Nx) — holding
    one switches the monitor into the fast, tight-stop cadence."""
    return [s for s in holdings
            if s in conf.HEDGE_INSTRUMENTS and not conf.HEDGE_INSTRUMENTS[s].get("holdable", True)]


def hedge_triggers(pos, market, cfg=None):
    """Tight risk triggers for any leveraged inverse hedge in the book (e.g. SQQQ
    -3x): a hard stop vs average cost, and an underlying-reversal warning (the
    inverse bleeds ~Nx when the underlying rips up). Pure."""
    cfg = cfg or conf.HEDGE_MONITOR
    fired = []
    by = {p.get("symbol"): p for p in (pos or {}).get("positions", [])}
    for sym, spec in conf.HEDGE_INSTRUMENTS.items():
        if spec.get("holdable", True) or sym not in by:
            continue
        lev = spec.get("leverage", 1)
        ind = rg.indicators(market.get(sym, {}))
        last, avg = ind.get("last"), by[sym].get("average_buy_price")
        if last and avg:
            pnl = last / avg - 1.0
            if pnl <= -cfg["stop_pct"]:
                fired.append({"kind": f"hedgestop:{sym}", "ticker": sym,
                    "detail": f"🛑 对冲止损 {sym}(-{lev}x) 已亏 {pnl:+.1%}（现价 ${last:,.2f}/成本 ${avg:,.2f}）"
                              f" — 杠杆反向ETF会衰减，按纪律离场"})
        und = spec.get("underlying")
        uind = rg.indicators(market.get(und, {}))
        umv = pct_move(uind.get("last"), uind.get("prev_close"))
        if umv is not None and umv >= cfg["underlying_jump"]:
            fired.append({"kind": f"hedgerev:{sym}", "ticker": sym,
                "detail": f"⚠️ {und} +{umv:.1%} → 你的 {sym}(-{lev}x) 正快速失血（约 {-lev*umv:.1%}）"
                          f"，重新评估对冲"})
    return fired


# ---- plan-level "good time to trade" triggers ---------------------------

def load_plan(date):
    """Today's persisted plan (entry/stop levels) written by the desk run."""
    try:
        return json.loads(conf.plan_path(date).read_text())
    except Exception:
        return []


def plan_triggers(plan, market, *, held=None, qty=None, near_pct=None):
    """Fire when intraday price reaches (or approaches) a planned entry/stop level
    from the day's report — the proactive 'good time to trade NOW' alerts.

    `held` is the LIVE held set and `qty` a LIVE {symbol: shares} map (both from
    the freshest snapshot). The plan file is written ONCE at preopen, so its trim
    size can be stale — if you've since trimmed the position, the old dollar amount
    is wrong and the trim is largely done. So exit/trim alerts (a) require the name
    still held, (b) RE-SIZE off your current position value, and (c) go quiet once
    the position is too small to trim meaningfully."""
    near_pct = conf.ALERT.get("near_entry_pct", 0.012) if near_pct is None else near_pct
    qty = qty or {}
    fired = []
    for c in plan or []:
        sym = c.get("ticker")
        ind = rg.indicators(market.get(sym, {}))
        last = ind.get("last")
        if last is None:
            continue
        act, zone, stop = c.get("action"), c.get("entry_zone"), c.get("stop")
        held_or_unknown = (held is None) or (sym in held)  # stops: fire even if book unknown (safety)
        known_held = held is not None and sym in held      # exits/entry-done: need POSITIVE knowledge
        amt = c.get("dollars")
        amt_s = f"，计划 ${amt:,.0f}" if amt else ""
        entry_ok = act == "BUY" or (act == "NEW_BUY" and not known_held)   # NEW_BUY done => quiet
        if entry_ok and act in ("BUY", "NEW_BUY") and zone and last <= zone * 1.005:
            fired.append({"kind": f"entry:{sym}", "ticker": sym,
                          "detail": f"✅ 可买入 {sym}：现价 ${last:,.2f} 已到入场区 ${zone:,.2f}{amt_s}"})
        elif entry_ok and act in ("BUY", "NEW_BUY") and zone and last <= zone * (1 + near_pct):
            fired.append({"kind": f"near:{sym}", "ticker": sym,
                          "detail": f"👀 接近入场 {sym}：现价 ${last:,.2f} 距入场区 ${zone:,.2f} "
                                    f"仅 {(last/zone-1)*100:+.1f}% — 准备好资金{amt_s}"})
        if stop and last <= stop and held_or_unknown:
            fired.append({"kind": f"stop:{sym}", "ticker": sym,
                          "detail": f"🛑 {sym} 跌破止损 ${stop:,.2f}（现价 ${last:,.2f}）— 考虑离场"})
        if act in ("TRIM", "SELL") and known_held:         # never nag to sell a name already gone
            pc = ind.get("prev_close")
            q = qty.get(sym)                                # None => live size unknown
            if pc and last >= pc * 1.02:
                verb = "减仓" if act == "TRIM" else "卖出"
                if q is None:                              # unknown size — fall back to the plan amount
                    fired.append({"kind": f"exit:{sym}", "ticker": sym,
                                  "detail": f"📈 {sym} 走强 +{(last/pc-1)*100:.1f}% — 可逢强{verb}{amt_s}"})
                else:
                    held_val = q * last                    # LIVE position value
                    plan_amt = c.get("dollars") or 0
                    # ALREADY-TRIMMED guard: if you now hold less than the morning
                    # plan wanted to trim, you've already done it — go quiet (the
                    # MRVL "keeps telling me to trim $ I don't have" bug).
                    already_done = act == "TRIM" and plan_amt and held_val < plan_amt
                    if held_val >= 5 and not already_done:
                        # re-size off the CURRENT position so a stale morning amount
                        # can never exceed what you actually hold now
                        live_amt = round(held_val if act == "SELL" else held_val * 0.33)
                        fired.append({"kind": f"exit:{sym}", "ticker": sym,
                                      "detail": f"📈 {sym} 走强 +{(last/pc-1)*100:.1f}% — 可逢强{verb} "
                                                f"约 ${live_amt:,.0f}（现持 ${held_val:,.0f}）"})
    return fired


# ---- dedupe state -------------------------------------------------------

def _load_positions():
    """The live book, or None if the file is missing/unreadable (a genuinely
    unknown book — distinct from a known-empty one)."""
    try:
        return json.loads(conf.POSITIONS_PATH.read_text())
    except Exception:
        return None


def _load_state():
    try:
        return json.loads(conf.STATE_PATH.read_text())
    except Exception:
        return {"date": "", "fired": []}


def _save_state(st):
    try:
        conf.STATE_PATH.write_text(json.dumps(st))
    except Exception:
        pass


# alert kinds that RE-FIRE while the condition persists (a 'good time to trade'
# ping is easy to miss); everything else stays once-per-day. `hedgerev:` is
# deliberately NOT here — a -3x hedge bleeding while the market rises is EXPECTED,
# not an action item, so it fires once/day, not every 15 min.
_REFIRE_PREFIXES = ("entry:", "near:", "stop:", "exit:", "hedgestop:")


def _fresh(triggers, state, date, *, now_min=None, refire_minutes=None):
    """Dedupe triggers. Informational kinds fire once per day; actionable kinds
    get a time-bucketed key so they re-fire while the condition still holds — a
    leveraged-hedge stop/reversal on the FAST `HEDGE_MONITOR` interval (default
    15min), everything else on the book-wide `ALERT` interval (default 90min).
    `now_min` = minutes since midnight (injectable)."""
    book_refire = conf.ALERT.get("refire_minutes", 90) if refire_minutes is None else refire_minutes
    hedge_refire = conf.HEDGE_MONITOR.get("refire_minutes", book_refire)
    if now_min is None:
        n = datetime.now(ET)
        now_min = n.hour * 60 + n.minute
    if state.get("date") != date:
        state["date"], state["fired"] = date, []
    seen = set(state["fired"])
    new = []
    for t in triggers:
        key = f"{date}:{t['kind']}:{t['ticker']}"
        if t["kind"].startswith(_REFIRE_PREFIXES):
            r = hedge_refire if t["kind"].startswith("hedgestop:") else book_refire
            if r:
                key += f":{int(now_min // r)}"
        if key not in seen:
            new.append(t)
            seen.add(key)
    state["fired"] = sorted(seen)
    return new


# ---- one tick -----------------------------------------------------------

def tick(holdings, *, cfg=None, dry=False, llm=False, date=None):
    cfg = cfg or conf.ALERT
    date = date or datetime.now(ET).strftime("%Y-%m-%d")
    plan = load_plan(date)
    plan_syms = [c.get("ticker") for c in plan if c.get("ticker")]
    # also price every hedge instrument + its underlying so the tight hedge watch works
    hedge_syms = list(conf.HEDGE_INSTRUMENTS) + [v["underlying"] for v in conf.HEDGE_INSTRUMENTS.values()]
    syms = list({conf.BENCH, conf.MOMENTUM_FACTOR, conf.MACRO_PANEL["rates_10y"],
                 conf.MACRO_PANEL["btc"], *holdings, *plan_syms, *hedge_syms})
    market = rg.fetch_market(syms, period="6mo")
    vix = rg.fetch_vix()
    pos_doc = _load_positions()

    # Watch the market with broker-grade prices: every tick, overlay Robinhood MCP
    # real-time quotes (read-only relay) for the whole watch set — holdings, today's
    # plan levels, and the hedge pair. yfinance daily bars lag ~15min, which is what
    # made 'good time to trade' alerts fire late. Falls back to yfinance 1m bars.
    watch_syms = list({*holdings, *plan_syms, *hedge_syms})
    live = {}
    if conf.ALERT.get("mcp_quotes") and not dry:
        live = SNAP.realtime_quotes(watch_syms)                  # broker real-time, {} on failure
    if not live and held_leveraged_hedges(holdings):
        try:
            intr = rg.fetch_market(hedge_syms, period="1d", interval="1m")
            live = {s: (intr.get(s, {}) or {}).get("closes", [None])[-1] for s in hedge_syms}
        except Exception:
            live = {}
    SNAP.overlay_live(market, live, today=date)    # live last + correct prev_close (date-aware)

    # the LIVE held set — plan alerts must respect what the user holds NOW. Three
    # states: a set (known book), empty set (known: holds nothing), or None (book
    # UNREADABLE — don't let a lost snapshot resurrect exit alerts for sold names).
    held = None if pos_doc is None else {
        p.get("symbol") for p in pos_doc.get("positions", []) if (p.get("quantity") or 0) > 0}
    qty = {} if pos_doc is None else {
        p.get("symbol"): (p.get("quantity") or 0.0) for p in pos_doc.get("positions", [])}

    triggers = []
    triggers += hedge_triggers(pos_doc, market, cfg=conf.HEDGE_MONITOR)   # tight -3x stop FIRST
    triggers += plan_triggers(plan, market, held=held, qty=qty)   # live-book aware + re-sized
    for s in holdings:
        triggers += name_triggers(s, rg.indicators(market.get(s, {})), cfg)
    triggers += macro_triggers(market, vix, cfg)
    leaders = [s for s in holdings if conf.is_momentum_bet(s)]
    triggers += unwind_trigger(L2.unwind_read(market, leaders, vix=vix))

    state = _load_state()
    fresh = _fresh(triggers, state, date)
    if dry:
        return {"all": triggers, "fresh": fresh, "sent": False}

    if fresh:
        body = "\n".join(f"• {t['detail']}" for t in fresh)
        if llm:
            sharpened = L6.enrich_with_llm({"date": date, "alerts": fresh},
                                           run_dir=BOT_DIR / "runs" / f"desk_alert_{date}",
                                           prompt="desk_alert.md")
            # NUMERIC GUARD: the LLM must not invent or alter a $ amount / % (it once
            # rewrote a $378 trim as "$3000"). Only use the sharpened text if every
            # figure in it already appears in the deterministic body; else send the
            # deterministic body verbatim.
            if sharpened and L6._tokens_preserved(sharpened, body):
                body = sharpened
        L6.send_imessage(f"{conf.MSG_PREFIX} ALERT {date}", body)
        _save_state(state)
    return {"all": triggers, "fresh": fresh, "sent": bool(fresh)}


def account_activity(*, dry=False):
    """Refresh the live book; if the user traded since the last snapshot, send a
    'trade monitored' alert and return (changes, new_holdings). Best-effort:
    a snapshot failure simply yields no changes (the desk note already warns on a
    stale book). Returns ([], None) when nothing changed."""
    ok, _msg = (False, "dry") if dry else SNAP.refresh()
    changes = SNAP.diff_since_prev() if ok else []
    if not changes:
        return [], None
    body = ["📒 已监控到你的交易（trade monitored）："]
    for a in changes[:8]:
        verb = _ACTIVITY_VERB_ZH.get(a["kind"], a["kind"])
        body.append(f"• {verb} {a['symbol']} → 现 {a['curr_qty']:g} 股（{a['delta']:+g}）")
    body.append("已按你的最新持仓重新评估风险与仓位。")
    if not dry:
        L6.send_imessage(f"{conf.MSG_PREFIX} 交易已监控", "\n".join(body))
    pos = json.loads(conf.POSITIONS_PATH.read_text()) if conf.POSITIONS_PATH.exists() else {}
    return changes, [p["symbol"] for p in pos.get("positions", [])] or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--dry", action="store_true", help="compute + print triggers, send nothing")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--interval", type=int, default=conf.ALERT["tick_seconds"])
    args = ap.parse_args()

    pos = json.loads(conf.POSITIONS_PATH.read_text()) if conf.POSITIONS_PATH.exists() else {}
    holdings = [p["symbol"] for p in pos.get("positions", [])] or list(conf.HOLDINGS)

    if args.once:
        print(json.dumps(tick(holdings, dry=args.dry, llm=args.llm), indent=2, default=str))
        return

    today = datetime.now(ET).strftime("%Y-%m-%d")
    H.write_heartbeat("monitor", date=today,
                      at=datetime.now(ET).isoformat(timespec="seconds"),
                      ok=True, warnings=["monitor started"])
    errors = 0
    ticks = 0
    while cal.is_open_now() and cal.seconds_to_close() > 60:
        try:
            # periodically re-read the live account so a trade you place mid-session
            # is detected, acknowledged ('trade monitored'), and re-analyzed
            if SNAP_EVERY and ticks % SNAP_EVERY == 0:
                changes, new_holdings = account_activity(dry=args.dry)
                if changes:
                    print(f"[{datetime.now(ET):%H:%M}] trade monitored: "
                          f"{[(c['symbol'], c['kind']) for c in changes]}", flush=True)
                    if new_holdings:
                        holdings = new_holdings
            ticks += 1
            res = tick(holdings, dry=args.dry, llm=args.llm)
            errors = 0
            if res["fresh"]:
                print(f"[{datetime.now(ET):%H:%M}] alerted: {[t['kind'] for t in res['fresh']]}", flush=True)
        except Exception as e:
            errors += 1
            print(f"tick error ({errors}): {e}", flush=True)
            if errors == 3 and not args.dry:          # alert once on sustained failure
                L6.send_imessage("⚠️ Desk monitor errors",
                                 f"{errors} consecutive monitor tick errors — latest: {e}")
        # holding a leveraged inverse (SQQQ -3x) => tighten to the per-minute watch
        lev = held_leveraged_hedges(holdings)
        interval = conf.HEDGE_MONITOR["tick_seconds"] if lev else args.interval
        if lev and ticks == 1:
            print(f"[{datetime.now(ET):%H:%M}] leveraged hedge held ({lev}) — "
                  f"tight {interval}s watch with hard stop", flush=True)
        time.sleep(interval)
    print("market closed — monitor exiting", flush=True)


if __name__ == "__main__":
    main()
