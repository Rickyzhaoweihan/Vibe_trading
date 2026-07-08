#!/usr/bin/env python3
"""The desk orchestrator — wires L1–L7 into a deliverable.

Modes:
  preopen  — full desk note + game plan (the primary daily deliverable)
  wrap     — after-close: same note refreshed + accountability review
  weekly   — deeper pass: also deep-researches the top scouted ideas

ADVISORY ONLY: this never places an order. The only subprocess it may spawn is a
read-only `claude -p` (no order tools) for optional thesis enrichment, and
notify.py for delivery.

  python bot/desk/desk.py --mode preopen
  python bot/desk/desk.py --mode wrap --no-research --no-notify   # cheap dry run
"""

from __future__ import annotations

import argparse
import json
import sys
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
import deepresearch
import signals
import snapshot as SNAP
import macro as L1
import sectors as L2
import portfolio as L5
import scout as L3
import research as L4
import journal as L7
import synthesize as L6
import health as H

ET = ZoneInfo("America/New_York")


def _all_symbols(holdings):
    return list({conf.BENCH, "QQQ", conf.MOMENTUM_FACTOR,
                 *conf.MACRO_PANEL.values(), *conf.GLOBAL_INDICES, *conf.FUTURES,
                 *conf.SECTOR_ETFS, *holdings, *conf.SCOUT_POOL, *conf.CRYPTO_MAJORS,
                 *conf.HEDGE_TICKERS})


def _index_move(market, sym):
    ind = rg.indicators(market.get(sym, {}))
    last, prev = ind.get("last"), ind.get("prev_close")
    return {"last": last, "pct": (last / prev - 1.0) if last and prev else None}


def market_pulse(market, sectors, date):
    """What the broad market did + the real headlines behind it. Index moves and
    sector leaders/laggards are deterministic (price data); the macro news digest
    is best-effort (empty if the news vendor is unavailable) — the --llm relay's
    web-searched 'why' is the reliable explanation layer on top."""
    return {
        "spy": _index_move(market, conf.BENCH),
        "qqq": _index_move(market, "QQQ"),
        "vix": _index_move(market, conf.MACRO_PANEL["vix"]),
        "leaders": sectors.get("leaders", []),
        "laggards": sectors.get("laggards", []),
        "news": signals.macro_digest(date),
    }


def _cheap_calls(holdings, market):
    """Research-free fallback: KEEP everything, flagging extension. Keeps dry
    runs and offline runs useful without spending LLM tokens."""
    calls = []
    for s in holdings:
        ext = L2.is_extended((market.get(s, {}) or {}).get("closes") or [])
        calls.append({"ticker": s, "held": True, "rating": "Hold", "action": "KEEP",
                      "stop_loss": None, "target": None,
                      "conviction": "low", "horizon": L4.horizon_of("KEEP"),
                      "reason": ("extended — watch for unwind" if ext else "technical hold")
                                + " (deep research skipped)", "ok": False})
    return calls


def run(mode, *, date=None, top=5, research=True, notify=True, llm=False):
    date = date or datetime.now(ET).strftime("%Y-%m-%d")
    pos = L5.load_positions()
    holdings = [p["symbol"] for p in pos.get("positions", [])] or list(conf.HOLDINGS)

    # detect the user's own trades since the last snapshot ('trade monitored')
    prev_doc = L5.load_positions(conf.POSITIONS_PREV_PATH) if conf.POSITIONS_PREV_PATH.exists() else None
    activity = L5.diff_positions(prev_doc, pos) if prev_doc else []

    # one market fetch shared by all layers
    market = rg.fetch_market(_all_symbols(holdings))
    vix = rg.fetch_vix()

    macro = L1.macro_read(market=market, vix=vix)
    macro.pop("_market", None)
    sectors = L2.sector_read(market)
    pulse = market_pulse(market, sectors, date)
    leaders = [s for s in holdings if conf.is_momentum_bet(s)]
    unwind = L2.unwind_read(market, leaders, vix=vix)
    ideas = L3.scout(held=set(holdings), market=market,
                     top_n=max(top, conf.SCOUT["research_n"]))

    # Price the book with BROKER real-time quotes (Robinhood MCP, read-only relay)
    # before anything is valued or sized. yfinance daily bars are yesterday's close
    # at the 08:00 preopen and ~15min stale intraday — trade amounts computed off
    # them are wrong. Falls back silently to yfinance if the relay fails; the
    # digest labels which price source was used so the user can tell.
    price_source = "yfinance(延迟)"
    if conf.ALERT.get("mcp_quotes"):
        live_syms = list({*holdings, *conf.HEDGE_TICKERS, conf.BENCH, "QQQ",
                          *[i["ticker"] for i in ideas.get("equity", [])[:4]]})
        live = SNAP.realtime_quotes(live_syms)
        if live:
            price_source = "Robinhood实时"
            SNAP.overlay_live(market, live, today=date)   # live last + correct prev_close

    pf = L5.portfolio_read(pos, market, macro=macro, unwind=unwind)
    # Deep-analysis gate for the -3x SQQQ: when the tape deteriorates enough to
    # consider it (elevated/high unwind or a risk-off regime), spend a research
    # confirm on the underlying (QQQ). hedge_plan.sqqq_decision then grades the
    # research rating against the risk signal to decide whether to lead with it.
    confirm_rating = None
    if research and L5.needs_sqqq_confirm(macro.get("label", "NEUTRAL"), unwind.get("band", "low")):
        try:
            cinfo = deepresearch.research(conf.HEDGE_INSTRUMENTS["SQQQ"]["underlying"],
                                          date, profile="confirm")
            confirm_rating = cinfo.get("rating")
        except Exception:
            confirm_rating = None        # confirm failed: graded decision falls back to -1x
    hedge = L5.hedge_plan(
        equity=pf.get("total_value", 0.0) - (pos.get("crypto_value", 0.0) or 0.0),
        regime_label=macro.get("label", "NEUTRAL"), unwind_band=unwind.get("band", "low"),
        current_net=pf.get("current_net", 1.0),
        net_target=macro.get("exposure", {}).get("net_target", 0.70),
        crowding=pf.get("crowding_share", 0.0), confirm_rating=confirm_rating)
    for o in hedge.get("options", []):       # attach a live price so the call is placeable
        o["price"] = rg.indicators(market.get(o["ticker"], {})).get("last")

    research_selected = []
    if research:
        cand_ideas = ideas.get("equity", [])[:conf.SCOUT["research_n"]]
        cov = L4.load_coverage()
        band = unwind.get("band", "low")
        pf_values = pf.get("values", {})
        pf_total = pf.get("total_value", 0.0) or 1.0
        traded_syms = {a["symbol"] for a in activity}          # names the user just traded
        full = mode in ("bootstrap", "weekly")                  # weekly/bootstrap = whole book

        if full:
            cand = [] if mode == "bootstrap" else [i["ticker"] for i in cand_ideas]
            targets = list(dict.fromkeys(holdings + cand))
            researched = L4.analyze_chunks(targets, date, held_set=set(holdings))
            research_selected = [{"ticker": r["ticker"],
                                  "held": r.get("held", r["ticker"] in set(holdings)),
                                  "reasons": ["full coverage"]} for r in researched]
        else:
            # DAILY: only names that EARN a deep run — catalyst / big move / just
            # traded / extension into unwind / never-covered / stale. Quiet names
            # carry their last verdict forward (no re-spend).
            edays = L4.earnings_days_map(holdings + [i["ticker"] for i in cand_ideas])
            items = []
            for s in holdings:
                st = L4.stale_days(s, date, cov)
                # already deep-researched today (e.g. at preopen) and not just
                # traded → reuse this morning's verdict; don't pay again at wrap
                if st == 0 and s not in traded_syms:
                    continue
                ind = rg.indicators(market.get(s, {}))
                mv = (ind["last"] / ind["prev_close"] - 1.0) if ind.get("last") and ind.get("prev_close") else None
                pv = L4.last_verdict(s, cov)     # if we've been telling the user to TRADE this, keep it fresh
                actionable_prior = bool(pv and pv.get("action") in ("BUY", "NEW_BUY", "TRIM", "SELL"))
                items.append({"ticker": s, "held": True, "move_pct": mv,
                              "extended": L2.is_extended((market.get(s, {}) or {}).get("closes") or []),
                              "weight": pf_values.get(s, 0.0) / pf_total,
                              "earnings_days": edays.get(s), "unwind_band": band,
                              "stale": st, "traded": s in traded_syms,
                              "actionable_prior": actionable_prior})
            for i in cand_ideas:
                items.append({"ticker": i["ticker"], "held": False, "scout_score": i.get("score"),
                              "earnings_days": edays.get(i["ticker"]), "unwind_band": band,
                              "stale": L4.stale_days(i["ticker"], date, cov)})
            research_selected = L4.select_for_research(
                items, max_n=conf.RESEARCH["max_daily"], min_score=conf.RESEARCH["min_score"])
            # analyze_chunks (not _parallel) so all selected names run even when
            # max_daily exceeds the per-batch cap of 6 — otherwise 7-8 would be
            # listed as researched but silently fall through to carried verdicts.
            researched = L4.analyze_chunks([r["ticker"] for r in research_selected],
                                           date, held_set=set(holdings))

        by = {r["ticker"]: r for r in researched}
        L4.mark_researched(researched, date)                    # store date + verdict per name
        # Each holding: fresh deep call if researched this run, else its last stored
        # verdict (carried forward), else a cheap technical KEEP.
        calls = []
        for s in holdings:
            if s in by:
                calls.append(by[s])
            elif (v := L4.last_verdict(s, cov)):
                d = L4.stale_days(s, date, cov)
                tag = "（今日深度研究）" if d == 0 else f"（沿用{d}天前深度研究）"
                c = {**v, "ticker": s, "held": True, "ok": False, "carried": True,
                     "reason": (v.get("reason") or "") + tag}
                # a stop/target computed off a several-day-old price is not a live
                # level — drop it past 2 days so the monitor doesn't watch a stale
                # stop (the name stays a KEEP, just without a monitored level)
                if d and d > 2:
                    c["stop_loss"], c["target"] = None, None
                calls.append(c)
            else:
                calls.append(_cheap_calls([s], market)[0])
        calls += [r for r in researched if not r.get("held")]   # scouted ideas researched this run
    else:
        calls = _cheap_calls(holdings, market)

    prices = {c["ticker"]: rg.indicators(market.get(c["ticker"], {})).get("last")
              for c in calls}

    # mark calls the desk has already been making (say "3rd day repeating") — done
    # BEFORE sizing so a repeated BUY-ADD can be made one-time, not a daily nag
    repeats = L7.repeat_counts(calls, L7.load_journal(), today=date)
    for c in calls:
        s = c["ticker"]
        if s in repeats:
            c["repeat_days"] = repeats[s]
        c["extended"] = L2.is_extended((market.get(s, {}) or {}).get("closes") or [])
        # Overweight adds are ONE-TIME, not daily: if we've already flagged a BUY-add
        # on a held name on a prior day, hold it instead of re-nagging (the empirical
        # win is in trims, not repeated adds).
        if c.get("action") == "BUY" and c.get("repeat_days"):
            c["action"] = "KEEP"
            c["reason"] = (c.get("reason") or "") + f"（已于前{c['repeat_days']}日建议加仓，持有观察）"

    # size + time every actionable call (dollars + shares + when), capped by the
    # cash actually in the account so the plan is directly placeable
    cash = pos.get("cash", 0.0) or 0.0
    cash_to_raise = L5.plan_trades(
        calls, values=pf.get("values", {}), total=pf.get("total_value", 0.0),
        net_target=macro.get("exposure", {}).get("net_target", 0.70),
        current_net=pf.get("current_net", 1.0), band=unwind.get("band", "low"),
        prices=prices, cash=cash)

    # ground every call in what the user ACTUALLY holds — live price, share count,
    # current market value — so an instruction can never overstate a position
    qty = {p.get("symbol"): (p.get("quantity") or 0.0) for p in pos.get("positions", [])}
    for c in calls:
        s = c["ticker"]
        c["price"] = prices.get(s)
        c["held_qty"] = qty.get(s)
        c["held_value"] = pf.get("values", {}).get(s)
        # DEFAULT STOP: research prose often omits a stop (esp. GLM); never leave a
        # held name unprotected — set a fallback from the live price (tighter for a
        # decaying leveraged inverse) so the monitor always has a level to watch.
        if c.get("stop_loss") is None and c.get("price") and c.get("held_qty"):
            lev = not conf.HEDGE_INSTRUMENTS.get(s, {}).get("holdable", True)
            c["stop_loss"] = round(c["price"] * (1 - (0.08 if lev else conf.DEFAULT_STOP_PCT)), 2)
            c["stop_default"] = True

    review = L7.review_outcomes()

    # health: warn on a stale/seed book, degraded data, or failed research instead
    # of shipping a confident-but-wrong note silently
    warnings = (H.check_positions(pos, date)
                + H.check_market(market, _all_symbols(holdings))
                + H.check_research(calls, research))

    context = {
        "date": date, "mode": mode, "macro": macro, "sectors": sectors,
        "unwind": unwind, "portfolio": pf, "ideas": ideas, "calls": calls,
        "review": review, "cash_to_raise": cash_to_raise, "warnings": warnings,
        "research_selected": research_selected, "activity": activity,
        "pulse": pulse, "hedge": hedge, "cash": cash,
        "price_source": price_source, "thesis": "",
    }

    if llm:
        run_dir = BOT_DIR / "runs" / f"desk_{date}_{mode}"
        thesis = L6.enrich_with_llm(context, run_dir=run_dir)
        if thesis:
            # the template already prints the H1 title; drop a leading one the
            # model may emit so the report doesn't double-title.
            lines = thesis.splitlines()
            while lines and (not lines[0].strip() or lines[0].lstrip().startswith("# ")):
                if lines[0].lstrip().startswith("# "):
                    lines.pop(0)
                    break
                lines.pop(0)
            context["thesis"] = "\n".join(lines).strip()

    # journal the actionable calls with an entry mark (today's price)
    for c in calls:
        ind = rg.indicators(market.get(c["ticker"], {}))
        c.setdefault("entry", ind.get("last"))
    L7.log_calls(calls, date=date)

    # persist the actionable plan (entry/stop levels) for the intraday monitor's
    # "good time to trade" alerts
    plan = [{"ticker": c["ticker"], "action": c.get("action"), "dollars": c.get("dollars"),
             "entry_zone": c.get("entry_zone"), "stop": c.get("stop_loss"),
             "when": c.get("when"), "last": c.get("entry")}
            for c in calls
            if c.get("action") in ("BUY", "NEW_BUY", "TRIM", "SELL") or c.get("stop_loss")]
    # preopen and wrap share plan_<date>.json; don't let a later run (e.g. wrap)
    # wipe a non-empty same-day plan the monitor is actively watching with an empty
    # one — that would kill all intraday entry/stop alerts for the rest of the day.
    try:
        pp = conf.plan_path(date)
        if plan or not pp.exists():
            pp.write_text(json.dumps(plan, indent=2))
        else:
            existing = json.loads(pp.read_text())
            if not existing:            # nothing worth preserving — safe to (re)write empty
                pp.write_text(json.dumps(plan, indent=2))
    except Exception:
        pass

    path, digest = L6.deliver(context, notify=notify)

    # heartbeat: a stale-book or no-data warning marks the run NOT ok so the
    # watchdog catches it (a wrong book is worse than no note)
    critical = any(w.startswith("POSITIONS") or "NOTHING" in w or "FAILED for ALL" in w
                   for w in warnings)
    H.write_heartbeat(mode, date=date, at=datetime.now(ET).isoformat(timespec="seconds"),
                      ok=not critical, warnings=warnings)
    return {"report": str(path), "digest": digest, "context": context, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preopen", "wrap", "weekly", "bootstrap"], default="preopen")
    ap.add_argument("--date")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--no-research", action="store_true",
                    help="skip the deep multi-agent pass (cheap/offline dry run)")
    ap.add_argument("--no-notify", action="store_true", help="don't send the iMessage digest")
    ap.add_argument("--llm", action="store_true", help="enrich thesis via read-only claude relay")
    args = ap.parse_args()
    try:
        out = run(args.mode, date=args.date, top=args.top,
                  research=not args.no_research, notify=not args.no_notify, llm=args.llm)
    except Exception as e:
        # never fail silently: alert, stamp a not-ok heartbeat, exit non-zero
        msg = f"{type(e).__name__}: {e}"
        if not args.no_notify:
            L6.send_imessage(f"⚠️ Desk {args.mode} CRASHED", msg)
        try:
            H.write_heartbeat(args.mode,
                              date=(args.date or datetime.now(ET).strftime("%Y-%m-%d")),
                              at=datetime.now(ET).isoformat(timespec="seconds"),
                              ok=False, warnings=[f"crash: {msg}"])
        except Exception:
            pass
        raise
    if out.get("warnings"):
        print("WARNINGS: " + "; ".join(out["warnings"]))
    print(f"wrote {out['report']}\n\n--- digest ---\n{out['digest']}")


if __name__ == "__main__":
    main()
