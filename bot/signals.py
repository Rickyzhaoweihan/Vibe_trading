#!/usr/bin/env python3
"""Cheap (no multi-agent) market context from TradingAgents' data tools, for
news/sentiment-aware brain routing.

These are data calls, not LLM debates — a few cheap fetches per day, cached.
Everything is best-effort: a missing API key or network error yields an empty
string so the brain still routes on price/vol alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "TradingAgents"))

_MACRO_CACHE = {}   # date -> digest
_SENT_CACHE = {}    # ticker -> digest
_TNEWS_CACHE = {}   # (ticker, date) -> digest


def _clip(text, n):
    text = (text or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


def macro_digest(date, *, look_back_days=3, limit=8, max_chars=1200):
    """A compact global-macro news digest for `date` (yyyy-mm-dd). Cached per
    day. Empty string if the configured news vendor is unavailable."""
    if date in _MACRO_CACHE:
        return _MACRO_CACHE[date]
    digest = ""
    try:
        from tradingagents.agents.utils.news_data_tools import get_global_news
        raw = get_global_news.invoke(
            {"curr_date": date, "look_back_days": look_back_days, "limit": limit})
        digest = _clip(raw, max_chars)
    except Exception:
        digest = ""
    _MACRO_CACHE[date] = digest
    return digest


def sentiment(ticker, *, limit=30, max_chars=500):
    """A compact StockTwits sentiment read for `ticker` (free, no API key).
    Empty string on failure."""
    if ticker in _SENT_CACHE:
        return _SENT_CACHE[ticker]
    digest = ""
    try:
        from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
        digest = _clip(fetch_stocktwits_messages(ticker, limit=limit), max_chars)
    except Exception:
        digest = ""
    _SENT_CACHE[ticker] = digest
    return digest


def ticker_news(ticker, date, *, look_back_days=7, max_chars=700):
    """A compact per-ticker news digest for `ticker` as of `date` (yyyy-mm-dd),
    from the same `get_news` tool the deep-research news analyst uses (yfinance,
    keyless). Cached per (ticker, date). Empty string on failure."""
    key = (ticker, date)
    if key in _TNEWS_CACHE:
        return _TNEWS_CACHE[key]
    digest = ""
    try:
        from datetime import date as _date, timedelta
        from tradingagents.agents.utils.news_data_tools import get_news
        end = date
        y, m, d = map(int, date.split("-"))
        start = (_date(y, m, d) - timedelta(days=look_back_days)).isoformat()
        raw = get_news.invoke({"ticker": ticker, "start_date": start, "end_date": end})
        digest = _clip(raw, max_chars)
    except Exception:
        digest = ""
    _TNEWS_CACHE[key] = digest
    return digest


def routing_context(date, tickers=("QQQ", "SPY"), *, max_chars=1600):
    """Bundle the macro digest + a couple of key-ticker sentiment reads into a
    single compact string for the brain's routing prompt."""
    parts = []
    macro = macro_digest(date)
    if macro:
        parts.append("MACRO NEWS:\n" + macro)
    for t in tickers:
        s = sentiment(t)
        if s:
            parts.append(f"{t} SENTIMENT:\n{s}")
    return _clip("\n\n".join(parts), max_chars)
