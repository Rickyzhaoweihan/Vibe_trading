#!/usr/bin/env python3
"""Desk configuration — universe, cross-asset panels, the hedge/expression menu,
cluster map, and tunable thresholds.

Pure data + a couple of pure helpers. No network, no imports of heavy modules,
so every other desk module and every test can import it freely.
"""

from __future__ import annotations

import os
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
ROOT = BOT_DIR.parent

REPORTS_DIR = BOT_DIR / "reports"
LOGS_DIR = BOT_DIR / "logs"
POSITIONS_PATH = DESK_DIR / "positions.json"   # live book; refreshed by the snapshot relay
POSITIONS_PREV_PATH = DESK_DIR / "positions_prev.json"  # prior snapshot, to diff for user trades
STATE_PATH = DESK_DIR / "state.json"           # desk-only (alert dedupe); NOT the bot's state.json
JOURNAL_PATH = LOGS_DIR / "desk_journal.jsonl"
COVERAGE_PATH = DESK_DIR / "coverage.json"     # {ticker: last deep-research date} for the 6/day rotation


def plan_path(date):
    """Today's machine-readable trade plan (calls + entry/stop levels) the
    intraday monitor watches for 'good time to trade' alerts."""
    return DESK_DIR / f"plan_{date}.json"

# The user's real book. Advisory-only: we READ this account; we never trade it.
# From .env (DESK_ACCOUNT) so the real id stays out of source.
ACCOUNT = os.environ.get("DESK_ACCOUNT", "")

# iMessage subject prefix for desk messages.
MSG_PREFIX = "Desk"

# Delivery language: "en" sends English as-is (default); "zh" translates the
# report to Simplified Chinese before sending. Override with DESK_LANG in .env.
DELIVER_LANG = os.environ.get("DESK_LANG", "en")

# ---- universes ----------------------------------------------------------

# Seed holdings (the orchestrator refreshes from the live account when a
# positions snapshot is available; this is the offline fallback).
HOLDINGS = ["NVDA", "MSFT", "GOOGL", "AMZN", "TSLA", "INTC", "MU", "SNDK",
            "MRVL", "QQQ", "VOO", "SPCX"]

BENCH = "SPY"                 # broad-market benchmark for relative strength
MOMENTUM_FACTOR = "MTUM"      # momentum-factor proxy (vs BENCH) for unwind reads

# L1 cross-asset macro panel (all free on yfinance).
MACRO_PANEL = {
    "rates_10y": "^TNX",
    "rates_30y": "^TYX",
    "rates_13w": "^IRX",
    "dollar": "DX-Y.NYB",
    "gold": "GC=F",
    "oil": "CL=F",
    "vix": "^VIX",
    "btc": "BTC-USD",
}
GLOBAL_INDICES = ["^KS11", "^N225", "^STOXX50E", "^HSI"]   # Korea, Japan, Europe, HK
FUTURES = ["ES=F", "NQ=F"]

# L2 sector ETFs.
SECTOR_ETFS = ["XLK", "SMH", "XLF", "XLE", "XLV", "XLY", "XLP",
               "XLI", "XLU", "XLB", "XLRE", "XLC"]
DEFENSIVE_SECTORS = ["XLP", "XLU", "XLV"]

# L1 macro-expression / hedge menu (advisory tickers, equities/ETFs only).
HEDGES = {
    "duration": ["TLT", "IEF"],     # long Treasuries — for a falling/peaking-rates view
    "dollar": ["UUP"],
    "gold": ["GLD"],
    "defensive": DEFENSIVE_SECTORS,
    "inverse": ["SH", "PSQ", "SQQQ"],
    "cash": ["CASH"],
}

# Inverse ETFs the desk can SIZE as a downside hedge (rise when the market falls).
# `leverage` is the inverse multiple; `holdable=False` means it decays from the
# daily reset / volatility drag and is a TACTICAL hedge only, never buy-and-hold.
# The book is Nasdaq/tech-heavy, so the QQQ-inverse pair hedges its actual beta
# better than the broad S&P inverse.
HEDGE_INSTRUMENTS = {
    "PSQ":  {"underlying": "QQQ", "leverage": 1, "holdable": True,
             "note": "-1x Nasdaq-100 — low decay, best 1x match for a tech-heavy book; OK to hold"},
    "SH":   {"underlying": "SPY", "leverage": 1, "holdable": True,
             "note": "-1x S&P 500 — low decay, broad-market hedge; OK to hold"},
    "SQQQ": {"underlying": "QQQ", "leverage": 3, "holdable": False,
             "note": "-3x Nasdaq-100 — DECAYS from daily reset/vol drag; tactical days-to-weeks only"},
}
HEDGE_TICKERS = list(HEDGE_INSTRUMENTS)

# Deep research is the dominant daily cost (~$0.20/name on GLM 5.2), so it runs
# ONLY when a name earns it: catalyst (earnings), big move, user just traded it,
# extension into unwind risk, a never-researched holding, or staleness beyond the
# refresh window. Quiet names carry forward their last verdict (coverage.json).
# The Sunday weekly still refreshes the whole book.
RESEARCH = {
    "min_score": int(os.environ.get("DESK_RESEARCH_MIN_SCORE", "25")),  # priority score a name needs
    "max_daily": int(os.environ.get("DESK_RESEARCH_MAX", "8")),         # hard cap per run
    "stale_refresh_days": 7,        # even a quiet name gets re-researched weekly
    # RESERVED deep-research slots for the top NEW scouted ideas each run — so an
    # interesting new name always earns the multi-agent analysis before you'd act
    # on it, instead of losing every slot to holdings.
    "reserve_ideas": int(os.environ.get("DESK_RESERVE_IDEAS", "2")),
}

# HBM / AI-memory complex — the user wants these deep-researched harder + more
# often (the AI-memory supercycle is the book's core thesis). Names here get a
# research-priority boost so they clear the gate and rotate through more.
HBM_FOCUS = {"MU", "SKHY", "SNDK", "NVDA", "AVGO", "MRVL"}

# IPO / new-listing entry watch. A brand-new listing has no price history, so the
# scout/deep-research pipeline can't rank it for weeks — this bridges the gap: the
# desk injects an entry-zone watch into the plan so the monitor pings you when the
# price pulls back to a disciplined entry (NOT the day-1 pop). Remove once the name
# has enough history to be researched normally.
IPO_WATCH = {
    "SKHY": {"ref_price": 158.26, "listed": "2026-07-10",
             "note": "SK Hynix IPO（HBM 龙头，62%份额）— 回调至发行价 ~$158 附近买入，勿追首日高开"},
}

# Fallback protective stop (fraction below live price) for a held name whose deep
# research didn't state one — so nothing the desk holds is ever left unmonitored.
DEFAULT_STOP_PCT = float(os.environ.get("DESK_DEFAULT_STOP_PCT", "0.12"))

# The user wants a STANDING downside hedge (not just a risk-off reaction). This is
# the floor hedge as a fraction of equity even in a calm/bull tape; it scales up
# from here as regime/unwind deteriorate. Set to 0.0 to make hedging purely
# reactive. Override with DESK_BASE_HEDGE.
BASE_HEDGE = float(os.environ.get("DESK_BASE_HEDGE", "0.05"))

# A leveraged inverse (SQQQ -3x) decays and moves ~3x, so the moment one is held
# the monitor ticks FAST and stops out quickly — a slow leash on a -3x is how you
# lose. These govern that tight watch.
HEDGE_MONITOR = {
    "tick_seconds": int(os.environ.get("DESK_HEDGE_TICK", "60")),  # per-minute while a -3x is held
    "stop_pct": 0.08,            # hard stop on the hedge itself: -8% from cost
    "underlying_jump": 0.015,    # underlying up >=1.5% intraday => hedge bleeding fast, reassess
    "max_hold_days": 3,          # -3x decay: nudge to reassess / roll if held longer
    "refire_minutes": 15,        # a breached -3x stop re-warns every 15min, not the book-wide 90
    # use the Robinhood MCP (broker real-time quotes, via a read-only relay) for the
    # hedge stop instead of delayed yfinance — only while a -3x is actually held, so
    # the relay cost is bounded to the moment it matters. DESK_HEDGE_REALTIME=0 disables.
    "realtime": os.environ.get("DESK_HEDGE_REALTIME", "1") == "1",
}

# Idea-scout tuning. `discover` pulls a live market screen (yfinance predefined
# screens) so fresh names outside the curated pool surface; `screens` are the
# predefined lists queried; `min_price` filters penny/illiquid junk.
SCOUT = {
    "discover": os.environ.get("DESK_SCOUT_DISCOVER", "1") == "1",
    "screens": ["day_gainers", "most_actives", "undervalued_growth_stocks"],
    "screen_count": 25,          # names pulled per screen
    "min_price": 5.0,
    "top_n": 8,                  # ideas surfaced (was 5) — user wants more
    "research_n": 6,             # top ideas handed to deep research (was 4)
    "crowded_penalty": 0.03,     # nudge already-crowded AI-semis ideas down so diversifiers surface
}

# Crypto majors the scout ranks (the book holds crypto).
CRYPTO_MAJORS = ["BTC-USD", "ETH-USD", "SOL-USD"]

# Candidate pool for the idea scout (liquid names; merged with universe.json AND a
# best-effort live market screen). Deliberately spans sectors BEYOND the user's
# crowded AI-semis complex so the scout can surface diversifying ideas, not just
# more of what's already 60%+ of the book.
SCOUT_POOL = [
    # AI / semis / hardware (the crowded complex — kept, but not the whole pool)
    "NVDA", "AVGO", "AMD", "TSM", "MU", "SMCI", "ANET", "MRVL", "VRT", "DELL",
    "PLTR", "CRWD", "ARM", "QCOM", "LRCX", "KLAC", "SKHY",
    # software / internet / comm
    "META", "AAPL", "NFLX", "AMZN", "GOOGL", "MSFT", "TSLA", "ORCL", "NOW",
    "PANW", "SNOW", "DDOG", "ADBE", "CRM", "UBER",
    # power / energy / utilities (the AI-power theme + diversifiers)
    "VST", "CEG", "NEE", "XOM", "CVX",
    # healthcare
    "LLY", "UNH", "ISRG", "VRTX", "REGN",
    # financials / consumer / industrials (diversifiers)
    "JPM", "GS", "V", "MA", "COST", "WMT", "MCD", "HD", "CAT", "GE",
]

# ---- cluster / factor map (for portfolio concentration & crowding) -------
# Which "bet" each holding really belongs to. The momentum/AI-semis complex is
# the crowded factor we watch for unwind risk.
CLUSTERS = {
    "memory":       ["MU", "SNDK", "SKHY"],
    "ai_semis":     ["NVDA", "MRVL", "INTC", "AVGO", "AMD", "TSM", "SMCI", "SMH"],
    "megacap_tech": ["MSFT", "GOOGL", "AMZN", "META", "AAPL", "NFLX"],
    "index_beta":   ["QQQ", "VOO", "SPY"],
    "tail":         ["TSLA", "SPCX"],
    "crypto":       ["BTC-USD", "ETH-USD", "SOL-USD"],
}
# Clusters that together make up the crowded momentum bet.
MOMENTUM_CLUSTERS = ["memory", "ai_semis", "megacap_tech"]

# ---- tunable thresholds (config knobs) ----------------------------------

UNWIND = {
    "rsi_overbought": 75.0,        # leader RSI14 above this == extended
    "ext_above_sma50": 0.12,       # >12% above 50DMA == extended
    "corr_crowded": 0.70,          # avg pairwise correlation of leaders
    "vix_stress": 22.0,            # VIX level that flags risk
    "mtum_rolling_over": -0.02,    # MTUM minus SPY return over lookback below this
    "lookback": 63,                # ~3 months
    "score_elevated": 40.0,        # 0..100 band cutoffs
    "score_high": 65.0,
}

ALERT = {
    "move_pct": 0.03,              # |intraday move vs prior close| to flag a name
    "tnx_bp_jump": 8.0,            # 10Y yield intraday jump (basis points)
    "vix_stress": 22.0,
    "btc_move_pct": 0.06,
    "tick_seconds": 300,           # intraday monitor cadence (user wants frequent alerts)
    "near_entry_pct": 0.012,       # price within 1.2% ABOVE an entry zone => heads-up alert
    # actionable alerts (entry/stop/exit/hedge) RE-FIRE while the condition persists,
    # once per this window — a one-shot "good time to trade" ping is easy to miss.
    "refire_minutes": 90,
    # use the Robinhood MCP for real-time quotes on the watch set each tick (one
    # read-only relay call per tick). DESK_MCP_QUOTES=0 falls back to yfinance-only.
    "mcp_quotes": os.environ.get("DESK_MCP_QUOTES", "1") == "1",
}

# Net-equity-exposure target by regime (fraction of book in risk assets).
EXPOSURE_BY_REGIME = {
    "RISK_ON_TREND": 0.90,
    "NEUTRAL": 0.70,
    "HIGH_VOL_CHOP": 0.50,
    "RISK_OFF_TREND": 0.30,
}


def cluster_of(symbol: str) -> str:
    """The cluster a symbol belongs to (default 'other')."""
    for name, syms in CLUSTERS.items():
        if symbol in syms:
            return name
    return "other"


def is_momentum_bet(symbol: str) -> bool:
    """True if the symbol is part of the crowded momentum/AI-semis complex."""
    return cluster_of(symbol) in MOMENTUM_CLUSTERS
