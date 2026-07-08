#!/usr/bin/env python3
"""Tier 0 — the regime sensor.

Pure-Python technical indicators plus a market-regime classifier. The compute
functions take plain lists of prices so they are fully unit-testable offline;
`fetch_market` is the only part that touches the network (yfinance).

No LLM, no orders, no state writes. Tiers 1+ consume the feature dict.
"""

from __future__ import annotations

import math

# Regime labels (ordered loosely bullish -> bearish / unstable)
RISK_ON_TREND = "RISK_ON_TREND"      # index above 200SMA, positive momentum
NEUTRAL = "NEUTRAL"                   # mixed / transitional
HIGH_VOL_CHOP = "HIGH_VOL_CHOP"      # elevated vol without a clean trend
RISK_OFF_TREND = "RISK_OFF_TREND"    # index below 200SMA, negative momentum

TRADING_DAYS = 252


# ---- pure indicators (operate on a list of closes, oldest -> newest) ----

def sma(closes, n):
    if len(closes) < n or n <= 0:
        return None
    return sum(closes[-n:]) / n


def ema(closes, n):
    if len(closes) < n or n <= 0:
        return None
    k = 2 / (n + 1)
    e = sum(closes[:n]) / n
    for px in closes[n:]:
        e = px * k + e * (1 - k)
    return e


def _wilder_rsi(closes, n):
    """Wilder's RSI over the last n periods. Returns 0..100 or None."""
    if len(closes) < n + 1 or n <= 0:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes, n=14):
    return _wilder_rsi(closes, n)


def daily_returns(closes):
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def realized_vol(closes, n=20):
    """Annualized realized volatility from the last n daily returns."""
    rets = daily_returns(closes)
    if len(rets) < n or n < 2:
        return None
    window = rets[-n:]
    mean = sum(window) / n
    var = sum((r - mean) ** 2 for r in window) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def total_return(closes, n):
    """Return over the last n bars (closes[-1] / closes[-1-n] - 1)."""
    if len(closes) < n + 1 or n <= 0:
        return None
    return closes[-1] / closes[-1 - n] - 1.0


def indicators(bars):
    """Compute the indicator bundle a policy needs from a single symbol's bars.

    `bars` is a dict with at least {"closes": [...]} and optionally
    "highs"/"lows" (oldest -> newest). Missing-data fields come back as None.
    """
    closes = bars.get("closes") or []
    highs = bars.get("highs") or []
    return {
        "last": closes[-1] if closes else None,
        "prev_close": closes[-2] if len(closes) >= 2 else None,
        "prev_high": highs[-2] if len(highs) >= 2 else None,
        "sma5": sma(closes, 5),
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "rsi2": rsi(closes, 2),
        "rsi14": rsi(closes, 14),
        "ret_20": total_return(closes, 20),
        "ret_63": total_return(closes, 63),    # ~3 months
        "realized_vol": realized_vol(closes, 20),
    }


# ---- regime classification (pure, given a market dict) ------------------

# Thresholds. Tunable; reflect.py may adjust copies in policies.json but the
# regime sensor itself stays deterministic.
VOL_HIGH_ANN = 0.35       # annualized realized vol above this == "high vol"
VIX_HIGH = 25.0
MOM_LOOKBACK = 63         # ~3-month momentum for the trend read


def compute_regime(market, *, index="QQQ", vix=None):
    """Classify the market regime from the index series.

    `market` maps symbol -> bars dict (see `indicators`). Returns
    {"label": str, "features": {...}} where features carries the raw numbers
    the brain and policies reason over.
    """
    idx_bars = market.get(index, {})
    ind = indicators(idx_bars)
    last, sma50, sma200 = ind["last"], ind["sma50"], ind["sma200"]
    mom = total_return(idx_bars.get("closes") or [], MOM_LOOKBACK)
    vol = ind["realized_vol"]

    above_200 = last is not None and sma200 is not None and last > sma200
    golden = sma50 is not None and sma200 is not None and sma50 > sma200
    mom_pos = mom is not None and mom > 0
    high_vol = (vol is not None and vol > VOL_HIGH_ANN) or (vix is not None and vix > VIX_HIGH)

    if above_200 and golden and mom_pos and not high_vol:
        label = RISK_ON_TREND
    elif (last is not None and sma200 is not None and last < sma200) and not mom_pos:
        label = RISK_OFF_TREND
    elif high_vol:
        label = HIGH_VOL_CHOP
    else:
        label = NEUTRAL

    return {
        "label": label,
        "features": {
            "index": index,
            "last": last,
            "sma50": sma50,
            "sma200": sma200,
            "above_200sma": above_200,
            "golden_cross": golden,
            "momentum_63d": mom,
            "realized_vol": vol,
            "vix": vix,
            "high_vol": high_vol,
            "rsi14": ind["rsi14"],
        },
    }


# ---- network layer (the only impure part) -------------------------------

def fetch_market(symbols, period="1y", interval="1d"):
    """Download daily bars for `symbols` via yfinance and return the market
    dict that compute_regime/indicators consume. Per-symbol failures degrade
    to an empty bars dict so one bad ticker never kills a tick."""
    import yfinance as yf

    market = {}
    try:
        data = yf.download(
            symbols, period=period, interval=interval,
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except Exception:
        return {s: {} for s in symbols}

    for s in symbols:
        try:
            if len(symbols) == 1:
                df = data
            else:
                df = data[s]
            cser = df["Close"].dropna()
            closes = [float(x) for x in cser.tolist()]
            highs = [float(x) for x in df["High"].dropna().tolist()]
            lows = [float(x) for x in df["Low"].dropna().tolist()]
            dates = [(idx.date().isoformat() if hasattr(idx, "date") else str(idx))
                     for idx in cser.index]
            market[s] = {"closes": closes, "highs": highs, "lows": lows, "dates": dates}
        except Exception:
            market[s] = {}
    return market


def fetch_vix():
    """Latest VIX close, or None on failure."""
    import yfinance as yf
    try:
        hist = yf.Ticker("^VIX").history(period="5d")["Close"].dropna()
        return float(hist.iloc[-1]) if len(hist) else None
    except Exception:
        return None
