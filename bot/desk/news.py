#!/usr/bin/env python3
"""Desk news/event layer — a compact, keyless news digest for the strategist.

Built entirely from tools the desk ALREADY has: the TradingAgents news tools
(`get_global_news` / `get_news`, the deep-research news analyst's own feeds, via
`signals.py`), StockTwits sentiment, and the yfinance earnings calendar. No
multi-agent debate, no LLM, no web plugin — just a few cached data fetches the
strategist reads as its "what's happening in the world" context.

Everything is best-effort: any failing source is simply omitted, and an empty
digest is fine (the strategist still has the deterministic desk analysis).

  python bot/desk/news.py --date 2026-07-14 --holdings NVDA MU
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import signals
import research as L4

ET = ZoneInfo("America/New_York")

# Macro catalysts the strategist should weigh; inlined so the model knows what to
# look for in the headlines even when a given day's feed is thin.
EVENT_WATCH = ["CPI/PCE inflation", "FOMC / Fed speakers", "jobs / NFP",
               "10Y Treasury yield", "US–Iran / geopolitics", "OPEC / oil"]


def _earnings_lines(tickers, date, *, within_days=10):
    """`- TICKER: earnings in Nd` lines for names reporting within the window."""
    try:
        y, m, d = map(int, date.split("-"))
        from datetime import date as _date
        edays = L4.earnings_days_map(list(dict.fromkeys(tickers)), today=_date(y, m, d))
    except Exception:
        edays = {}
    lines = []
    for t, n in sorted(edays.items(), key=lambda kv: kv[1]):
        if n is not None and 0 <= n <= within_days:
            lines.append(f"- {t}: earnings in {n}d")
    return lines


def _safe(fn, default=""):
    """Call a best-effort source, swallowing any failure to a default. Even though
    the signals.* fetchers already guard themselves, we never let one bad source
    take down the whole digest."""
    try:
        return fn()
    except Exception:
        return default


def event_digest(date, holdings, ideas=(), *, per_name=4, max_chars=3800):
    """A compact news/event digest string for `date`. Sections, ordered
    high-signal-first so the verbose news is the part that gets trimmed if the
    budget is tight: EVENT WATCH, UPCOMING EARNINGS, SENTIMENT, MACRO NEWS,
    PER-NAME NEWS. Each section is clipped to its own budget. Never raises."""
    holdings = list(dict.fromkeys(holdings or []))
    ideas = list(dict.fromkeys(ideas or []))
    parts = ["## EVENT WATCH (weigh these catalysts)\n" + ", ".join(EVENT_WATCH)]

    elines = _safe(lambda: _earnings_lines(holdings + ideas, date), [])
    if elines:
        parts.append("## UPCOMING EARNINGS\n" + "\n".join(elines))

    sent_blocks = []
    for t in dict.fromkeys(["QQQ", "SPY"] + holdings[:2]):
        s = _safe(lambda t=t: signals.sentiment(t))
        if s:
            sent_blocks.append(f"{t}: {s}")
    if sent_blocks:
        parts.append("## SENTIMENT (StockTwits)\n" + "\n".join(sent_blocks))

    macro = _safe(lambda: signals.macro_digest(date))
    if macro:
        parts.append("## MACRO NEWS\n" + signals._clip(macro, 1000))

    per = holdings[:per_name] + [t for t in ideas if t not in holdings][:2]
    name_blocks = []
    for t in per:
        tn = _safe(lambda t=t: signals.ticker_news(t, date, max_chars=400))
        if tn:
            name_blocks.append(f"### {t}\n{tn}")
    if name_blocks:
        parts.append("## PER-NAME NEWS\n" + "\n\n".join(name_blocks))

    return signals._clip("\n\n".join(parts), max_chars)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--holdings", nargs="*", default=["NVDA", "MU", "ANET"])
    ap.add_argument("--ideas", nargs="*", default=[])
    args = ap.parse_args()
    print(event_digest(args.date, args.holdings, args.ideas))


if __name__ == "__main__":
    main()
