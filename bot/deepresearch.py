#!/usr/bin/env python3
"""Tier 3 — the TradingAgents multi-agent stack, wrapped for two callers:

  - the weekly CORE researcher (analyze.py): deep, quality config on 3-5 names.
  - the daemon's deep-CONFIRM gate: a cheaper run on a leveraged ETF's
    underlying index to veto obviously-bad aggressive entries.

Everything here is best-effort: a research failure returns a neutral result so
it never blocks trading. The graph is heavy, so instances are cached per
config and results cached per (ticker, date, profile).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent
sys.path.insert(0, str(ROOT / "TradingAgents"))  # repo on path; deps in .venv

# LLM provider/models for the research stack, overridable from .env so the user
# can trade cost vs quality without code changes. Defaults = the original
# Anthropic setup; current .env points at GLM 5.2 via OpenRouter (~6-8x cheaper
# than Opus). The provider's API key env var is resolved by TradingAgents
# (openrouter -> OPENROUTER_API_KEY, anthropic -> ANTHROPIC_API_KEY, ...).
LLM_PROVIDER = os.environ.get("BOT_LLM_PROVIDER", "anthropic")
DEEP_LLM = os.environ.get("BOT_DEEP_LLM", "claude-opus-4-8")
QUICK_LLM = os.environ.get("BOT_QUICK_LLM", "claude-sonnet-4-6")

# Leveraged ETF -> the underlying the deep-confirm gate actually researches.
UNDERLYING = {
    "TQQQ": "QQQ", "SQQQ": "QQQ",
    "SOXL": "SOXX", "SOXS": "SOXX",
    "TECL": "XLK", "TECS": "XLK",
    "UPRO": "SPY", "SPXU": "SPY",
    "FNGU": "QQQ", "FNGD": "QQQ",
}

# A research rating at or below this tier vetoes a long entry.
BEARISH = {"Sell", "Underweight"}

_GRAPHS = {}     # profile -> TradingAgentsGraph
_CACHE = {}      # (ticker, date, profile) -> research dict


def underlying_of(sym):
    """The index ticker to research for a (possibly leveraged) symbol."""
    return UNDERLYING.get(sym, sym)


def _config(profile):
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    common = dict(
        llm_provider=LLM_PROVIDER,
        results_dir=str(BOT_DIR / "logs" / "tradingagents"),
        memory_log_path=str(BOT_DIR / "logs" / "decision_memory.md"),
        memory_log_max_entries=500,
    )
    if profile == "core":
        # quality: deep model for synthesis, quick model for sub-agents
        common.update(deep_think_llm=DEEP_LLM, quick_think_llm=QUICK_LLM,
                      max_debate_rounds=1, max_risk_discuss_rounds=1)
    else:  # "confirm" — cheap & fast for the latency-sensitive entry gate
        common.update(deep_think_llm=QUICK_LLM, quick_think_llm=QUICK_LLM,
                      max_debate_rounds=1, max_risk_discuss_rounds=1)
    cfg.update(common)
    return cfg


def _analysts(profile):
    # core: full panel incl. the social/sentiment analyst (StockTwits + Reddit +
    # news, on the cheap quick LLM). confirm stays lean for the latency-sensitive
    # entry gate.
    return (["market", "social", "news", "fundamentals"] if profile == "core"
            else ["market", "news"])


def _graph(profile):
    if profile not in _GRAPHS:
        _GRAPHS[profile] = new_graph(profile)
    return _GRAPHS[profile]


def new_graph(profile):
    """A FRESH TradingAgentsGraph instance. The cached `_graph` singleton is not
    safe to call concurrently from multiple threads, so the parallel research
    runner builds one of these per worker thread."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    return TradingAgentsGraph(selected_analysts=_analysts(profile), config=_config(profile))


def _first_price(patterns, text):
    """First dollar figure matched by any of `patterns` (case-insensitive).
    Strips thousands-separator commas first so '$1,023' parses as 1023."""
    import re
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _parse_stop(text):
    """Pull a stop-loss level. Conservative: every pattern requires an adjacent
    '$' so loose prose ('stop ... 2.17%') can't yield a bogus number — a wrong
    stop is worse than a blank one. Handles 'stop loss $X', 'stop at/near $X',
    and the trailing '$X stop' form the synthesis prose tends to use."""
    return _first_price([
        r"stop[\s\-]?loss\b[^$\n]{0,15}\$\s*([0-9]+(?:\.[0-9]+)?)",
        r"stop\b\s*(?:loss|out)?\s*(?:at|near|of|to|:|=|around)?\s*\$\s*([0-9]+(?:\.[0-9]+)?)",
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*stop\b",
    ], text)


def _parse_target(text):
    """Pull a price target. Requires an adjacent '$'. Handles 'price target $X',
    'target price $X', 'target $X', 'upside target $X', and '$X target'."""
    return _first_price([
        r"(?:price[\s\-]?target|target[\s\-]?price|upside[\s\-]?target|target)\b[^$\n]{0,15}\$\s*([0-9]+(?:\.[0-9]+)?)",
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:price[\s\-]?)?target\b",
    ], text)


def research(ticker, date, *, profile="core", graph=None):
    """Run the multi-agent stack on `ticker`. Returns a dict:
       {ticker, rating, stop_loss, final_decision, trader_plan, ok}.
    On any failure returns a neutral Hold with ok=False (never raises).

    Pass `graph` (a per-thread `new_graph()` instance) to run in parallel; with
    `graph=None` the cached single-threaded singleton is used."""
    key = (ticker, date, profile)
    if key in _CACHE:
        return _CACHE[key]
    from tradingagents.agents.utils.rating import parse_rating
    import re
    try:
        final_state, _ = (graph or _graph(profile)).propagate(ticker, date)
        decision_text = final_state.get("final_trade_decision", "") or ""
        trader_plan = final_state.get("trader_investment_plan", "") or ""
        rating = parse_rating(decision_text)
        # the trader plan often omits levels; the risk-managed final decision is
        # where stop/target usually land, so scan BOTH.
        levels_text = (trader_plan + "\n" + decision_text)
        out = {
            "ticker": ticker, "rating": rating,
            "stop_loss": _parse_stop(levels_text),
            "target": _parse_target(levels_text),
            "final_decision": decision_text, "trader_plan": trader_plan, "ok": True,
        }
    except Exception as e:
        out = {"ticker": ticker, "rating": "Hold", "stop_loss": None, "target": None,
               "final_decision": "", "trader_plan": "", "ok": False, "error": str(e)}
    _CACHE[key] = out
    return out


def confirm_entry(lev_ticker, date):
    """Deep-confirm gate for an aggressive long entry. Researches the
    underlying index and returns (allow: bool, info: dict). Fails OPEN — a
    research error allows the trade (logs ok=False) rather than silently
    blocking the aggressive engine the user explicitly wants."""
    idx = underlying_of(lev_ticker)
    info = research(idx, date, profile="confirm")
    if not info["ok"]:
        return True, info            # fail open
    allow = info["rating"] not in BEARISH
    return allow, info
