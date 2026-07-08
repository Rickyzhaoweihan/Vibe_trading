#!/usr/bin/env python3
"""L4 — Single-name research.

Wraps the TradingAgents multi-agent stack (deepresearch.research) — the desk's
firm-of-analysts — and maps its 5-tier rating into a clear, single-word action
for the user: BUY / KEEP / TRIM / SELL for holdings, NEW_BUY / PASS for
candidates. Attaches a sentiment read and the trader plan as the reason.

deepresearch is heavy (LLM + network); only rating_to_action is pure/tested.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import deepresearch
import signals

ET = ZoneInfo("America/New_York")


def rating_to_action(rating, *, held=True):
    """Map a 5-tier research rating to a clear action.

    Held names get a position decision; candidates get an entry decision."""
    if held:
        return {"Buy": "BUY", "Overweight": "BUY", "Hold": "KEEP",
                "Underweight": "TRIM", "Sell": "SELL"}.get(rating, "KEEP")
    return {"Buy": "NEW_BUY", "Overweight": "NEW_BUY"}.get(rating, "PASS")


_CONVICTION = {"Buy": "high", "Sell": "high", "Overweight": "medium",
               "Underweight": "medium", "Hold": "low"}


def conviction_of(rating, *, ok=True):
    """How much weight to put behind the call. A technical-only fallback (no deep
    research) is always low-conviction, regardless of the nominal rating."""
    return _CONVICTION.get(rating, "low") if ok else "low"


def horizon_of(action):
    """Plain-language holding/timing horizon for a call."""
    return {"SELL": "exit now / near-term", "TRIM": "near-term de-risk",
            "BUY": "multi-week swing", "NEW_BUY": "multi-week swing",
            "KEEP": "hold — multi-week"}.get(action, "multi-week")


def _reason(info, sent):
    plan = (info.get("trader_plan") or info.get("final_decision") or "").strip()
    plan = " ".join(plan.split())
    if len(plan) > 280:
        plan = plan[:280] + "…"
    tail = f" | sentiment: {sent}" if sent else ""
    return (plan or "no research text") + tail


def analyze_ticker(ticker, date, *, held=True, graph=None):
    """Deep-research one name → {ticker, action, rating, stop_loss, reason, ...}.
    `graph` is an optional per-thread graph instance for parallel runs."""
    info = deepresearch.research(ticker, date, profile="core", graph=graph)
    sent = signals.sentiment(ticker)
    if sent and len(sent) > 160:
        sent = sent[:160] + "…"
    rating = info.get("rating", "Hold")
    ok = info.get("ok", False)
    action = rating_to_action(rating, held=held)
    return {
        "ticker": ticker,
        "held": held,
        "rating": rating,
        "action": action,
        "stop_loss": info.get("stop_loss"),
        "target": info.get("target"),
        "conviction": conviction_of(rating, ok=ok),
        "horizon": horizon_of(action),
        "reason": _reason(info, sent),
        "ok": ok,
        "error": info.get("error"),     # the real failure cause, surfaced not swallowed
    }


def analyze_book(tickers, date, *, held_set=None):
    held_set = set(held_set if held_set is not None else tickers)
    out = []
    for t in tickers:
        try:
            out.append(analyze_ticker(t, date, held=(t in held_set)))
        except Exception as e:                                  # never let one name kill the pass
            out.append({"ticker": t, "held": t in held_set, "rating": "Hold",
                        "action": "KEEP" if t in held_set else "PASS",
                        "stop_loss": None, "reason": f"research error: {e}", "ok": False})
    return out


# ---- selection strategy: which <=6 names earn an (expensive) deep run -------

MAX_RESEARCH = 6


def research_priority(*, held=True, move_pct=None, extended=False, weight=0.0,
                      earnings_days=None, unwind_band="low", scout_score=None,
                      stale="skip", traded=False, actionable_prior=False):
    """Pure priority score for spending a deep-research slot on a name.

    Ranks by catalyst (imminent earnings), today's move, extension into an
    unwind-risk regime, staleness (never / long-ago researched), and — for
    holdings — position size; candidates enter the pool at a moderate base so a
    flagged holding always outranks a quiet idea. `stale` is days since the last
    deep research (None = never), or "skip" to ignore the coverage signal.
    `actionable_prior` = the carried verdict tells the user to trade (BUY/TRIM/
    SELL): those must stay FRESH so the instruction is feasible against the live
    price — a KEEP is safe to carry, a trade call is not.
    """
    score, reasons = 0.0, []
    if actionable_prior and stale not in ("skip", None, 0):
        score += 28; reasons.append("上次为可执行建议，需按现价复核")   # keep trade calls current
    if traded:
        score += 30; reasons.append("刚交易过")                # the user just traded it — re-read it
    if stale != "skip":
        if stale is None:
            score += 35; reasons.append("未深度研究")          # never covered — cover it
        elif stale >= conf.RESEARCH.get("stale_refresh_days", 7):
            score += 25; reasons.append(f"{stale}天未更新")     # stale — refresh it
        elif stale >= 3:
            score += 12
    if earnings_days is not None and earnings_days >= 0:
        if earnings_days <= 2:
            score += 50; reasons.append(f"earnings in {earnings_days}d")
        elif earnings_days <= 7:
            score += 28; reasons.append(f"earnings in {earnings_days}d")
    if move_pct is not None:
        amv = abs(move_pct)
        if amv >= 0.05:
            score += 30; reasons.append(f"moved {move_pct:+.0%}")
        elif amv >= 0.03:
            score += 15; reasons.append(f"moved {move_pct:+.0%}")
    if extended:
        if unwind_band in ("elevated", "high"):
            score += 20; reasons.append("extended into unwind risk")
        else:
            score += 8; reasons.append("extended")
    if held and weight:
        score += min(20.0, weight * 120.0)        # ~12 pts at 10% of book, capped 20
        if weight >= 0.10:
            reasons.append(f"{weight:.0%} of book")
    if (not held) and scout_score is not None:
        score += 18.0
        reasons.append("new idea")
    return round(score, 1), reasons


def select_for_research(items, *, max_n=MAX_RESEARCH, min_score=0.0):
    """Rank candidate items and return the ones that EARN a deep run: score at
    least `min_score`, capped at `max_n`. With min_score=0 this is a pure top-N;
    with a real threshold a quiet day selects few or no names — that's the point
    (research is the dominant cost; only spend when a signal justifies it).

    `items` is a list of dicts with any of: ticker, held, move_pct, extended,
    weight, earnings_days, unwind_band, scout_score, stale, traded.
    """
    scored = []
    for it in items:
        s, reasons = research_priority(
            held=it.get("held", True), move_pct=it.get("move_pct"),
            extended=it.get("extended", False), weight=it.get("weight", 0.0),
            earnings_days=it.get("earnings_days"), unwind_band=it.get("unwind_band", "low"),
            scout_score=it.get("scout_score"), stale=it.get("stale", "skip"),
            traded=it.get("traded", False), actionable_prior=it.get("actionable_prior", False))
        if s >= min_score:
            scored.append({"ticker": it["ticker"], "held": it.get("held", True),
                           "score": s, "reasons": reasons})
    scored.sort(key=lambda x: (x["score"], x["held"]), reverse=True)
    return scored[:max_n]


# ---- parallel deep research (per-worker graph instances) --------------------

_tls = threading.local()


def _init_worker(profile):
    try:
        _tls.graph = deepresearch.new_graph(profile)
    except Exception:
        _tls.graph = None


def analyze_book_parallel(tickers, date, *, held_set=None, max_workers=MAX_RESEARCH):
    """Deep-research up to MAX_RESEARCH names concurrently, one graph per worker
    thread (the cached singleton is not thread-safe). Hard-capped at 6."""
    held_set = set(held_set if held_set is not None else tickers)
    tickers = list(dict.fromkeys(tickers))[:MAX_RESEARCH]      # de-dupe + hard cap
    if not tickers:
        return []
    workers = max(1, min(max_workers, MAX_RESEARCH, len(tickers)))

    def work(t):
        try:
            return analyze_ticker(t, date, held=(t in held_set),
                                  graph=getattr(_tls, "graph", None))
        except Exception as e:
            return {"ticker": t, "held": t in held_set, "rating": "Hold",
                    "action": "KEEP" if t in held_set else "PASS", "stop_loss": None,
                    "reason": f"research error: {e}", "ok": False}

    with ThreadPoolExecutor(max_workers=workers, initializer=_init_worker,
                            initargs=("core",)) as ex:
        return list(ex.map(work, tickers))


# ---- coverage ledger: last research date + last VERDICT per name ------------
# Entries are {ticker: {"date": "YYYY-MM-DD", "verdict": {...call fields...}}}.
# The verdict lets a quiet name (not re-researched today) carry its prior deep
# call forward instead of re-spending on it — the core cost saving. Legacy
# entries were bare date strings; readers below tolerate both.

# fields worth carrying forward from a deep call
_VERDICT_FIELDS = ("rating", "action", "stop_loss", "target", "conviction",
                   "horizon", "reason", "held")


def load_coverage():
    try:
        return json.loads(conf.COVERAGE_PATH.read_text())
    except Exception:
        return {}


def _entry_date(entry):
    """Date string from a ledger entry (dict new-form or bare string legacy)."""
    if isinstance(entry, dict):
        return entry.get("date")
    return entry if isinstance(entry, str) else None


def mark_researched(results, date):
    """Record date + verdict for each successfully-researched result. Accepts a
    list of result dicts (preferred) or bare ticker strings (date only)."""
    cov = load_coverage()
    for r in results:
        if isinstance(r, str):
            cov[r] = {"date": date, "verdict": (cov.get(r) or {}).get("verdict")}
            continue
        if not r.get("ok"):
            continue
        cov[r["ticker"]] = {"date": date,
                            "verdict": {k: r.get(k) for k in _VERDICT_FIELDS}}
    try:
        conf.COVERAGE_PATH.write_text(json.dumps(cov, indent=2))
    except Exception:
        pass


def last_verdict(ticker, cov=None):
    """The most recent stored deep verdict for `ticker`, or None."""
    cov = cov if cov is not None else load_coverage()
    entry = cov.get(ticker)
    return entry.get("verdict") if isinstance(entry, dict) else None


def stale_days(ticker, today, cov=None):
    """Days since `ticker` was last deep-researched, None if never."""
    cov = cov if cov is not None else load_coverage()
    d = _entry_date(cov.get(ticker))
    if not d:
        return None
    try:
        from datetime import date as _date
        last = _date(*map(int, d.split("-")))
        now = _date(*map(int, today.split("-")))
        return (now - last).days
    except Exception:
        return None


def analyze_chunks(tickers, date, *, held_set=None):
    """Deep-research ALL given names (the initial bootstrap), in parallel chunks
    of MAX_RESEARCH so we never exceed the per-batch concurrency / rate limits."""
    held_set = set(held_set if held_set is not None else tickers)
    tickers = list(dict.fromkeys(tickers))
    out = []
    for i in range(0, len(tickers), MAX_RESEARCH):
        out += analyze_book_parallel(tickers[i:i + MAX_RESEARCH], date, held_set=held_set)
    return out


def earnings_days_map(tickers, *, today=None):
    """Best-effort {ticker: days_until_next_earnings} via yfinance. Missing /
    unavailable names are simply absent. Never raises."""
    out = {}
    try:
        import yfinance as yf
        from datetime import date as _date
        td = today or datetime.now(ET).date()
        for t in tickers:
            try:
                cal = yf.Ticker(t).calendar
                ed = None
                if isinstance(cal, dict):
                    v = cal.get("Earnings Date")
                    ed = (v[0] if isinstance(v, (list, tuple)) and v else v)
                if hasattr(ed, "date"):
                    ed = ed.date()
                if isinstance(ed, _date):
                    out[t] = (ed - td).days
            except Exception:
                continue
    except Exception:
        return out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=conf.HOLDINGS)
    ap.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--no-notify", action="store_true", help="(no-op; research never notifies)")
    args = ap.parse_args()
    print(json.dumps(analyze_book(args.tickers, args.date), indent=2, default=str))


if __name__ == "__main__":
    main()
