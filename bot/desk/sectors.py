#!/usr/bin/env python3
"""L2 — Sector / market structure + the momentum-unwind lens.

Two jobs:
  1. Rank sectors by relative strength and read rotation/breadth (where money
     is flowing) — feeds idea hunting.
  2. Score the risk that the crowded momentum trade UNWINDS — the book's main
     tail. Signals: the momentum factor (MTUM) fading vs the market, the leaders
     getting overextended, leaders becoming highly correlated (crowded into one
     bet), and the classic deleveraging tell (VIX up + breadth diverging).

All scoring is pure and unit-tested on synthetic calm/crash series.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import regime as rg


# ---- pure stats ---------------------------------------------------------

def pearson(a, b):
    """Pearson correlation of two equal-length sequences, or None."""
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def avg_pairwise_corr(series_list):
    """Mean pairwise Pearson correlation across a list of return series."""
    vals = []
    for i in range(len(series_list)):
        for j in range(i + 1, len(series_list)):
            c = pearson(series_list[i], series_list[j])
            if c is not None:
                vals.append(c)
    return sum(vals) / len(vals) if vals else None


def excess_return(market, sym, bench, lookback):
    rs = rg.total_return((market.get(sym, {}) or {}).get("closes") or [], lookback)
    rb = rg.total_return((market.get(bench, {}) or {}).get("closes") or [], lookback)
    if rs is None or rb is None:
        return None
    return rs - rb


def is_extended(closes, cfg=None):
    """True if a name is stretched: RSI14 overbought OR far above its 50DMA."""
    cfg = cfg or conf.UNWIND
    ind = rg.indicators({"closes": closes})
    last, s50, r14 = ind["last"], ind["sma50"], ind["rsi14"]
    if r14 is not None and r14 >= cfg["rsi_overbought"]:
        return True
    if last is not None and s50:
        return (last - s50) / s50 >= cfg["ext_above_sma50"]
    return False


# ---- the unwind-risk score (pure) ---------------------------------------

def unwind_risk_score(*, mtum_minus_spy=None, frac_extended=0.0, avg_corr=None,
                      vix=None, breadth_divergence=False, cfg=None):
    """Combine the unwind signals into a 0..100 score + band + reasons.

    Higher = the crowded momentum book is more vulnerable to a sharp reversal.
    """
    cfg = cfg or conf.UNWIND
    score = 0.0
    reasons = []

    # 1) momentum-factor leadership fading (max 30)
    if mtum_minus_spy is not None:
        if mtum_minus_spy < cfg["mtum_rolling_over"]:
            score += 30
            reasons.append(f"momentum factor lagging market ({mtum_minus_spy:+.1%}) — leadership fading")
        elif mtum_minus_spy < 0:
            score += 15
            reasons.append("momentum factor soft vs market")

    # 2) leaders overextended (max 25)
    frac = max(0.0, min(1.0, frac_extended))
    score += 25 * frac
    if frac >= 0.5:
        reasons.append(f"{frac:.0%} of leaders overextended (RSI / above 50DMA)")

    # 3) crowding via correlation (max 25)
    if avg_corr is not None:
        if avg_corr >= cfg["corr_crowded"]:
            score += 25
            reasons.append(f"leaders highly correlated ({avg_corr:.2f}) — crowded into one bet")
        elif avg_corr >= cfg["corr_crowded"] - 0.10:
            score += 12

    # 4) deleveraging tell: vol + breadth (max 20)
    if vix is not None and vix >= cfg["vix_stress"]:
        score += 10
        reasons.append(f"VIX elevated ({vix:.0f})")
    if breadth_divergence:
        score += 10
        reasons.append("breadth diverging from index — narrow leadership")

    score = round(min(100.0, score), 1)
    band = ("high" if score >= cfg["score_high"]
            else "elevated" if score >= cfg["score_elevated"] else "low")
    return {"score": score, "band": band, "reasons": reasons}


# ---- orchestrators ------------------------------------------------------

def sector_read(market, *, lookback=63):
    """Rank sectors by excess return vs the benchmark; surface leaders/laggards."""
    ranked = []
    for s in conf.SECTOR_ETFS:
        er = excess_return(market, s, conf.BENCH, lookback)
        if er is not None:
            ranked.append({"sector": s, "excess": round(er, 4)})
    ranked.sort(key=lambda x: x["excess"], reverse=True)
    return {
        "ranked": ranked,
        "leaders": [r["sector"] for r in ranked[:3]],
        "laggards": [r["sector"] for r in ranked[-3:]],
    }


def unwind_read(market, leaders, *, vix=None, cfg=None):
    """Compute the unwind-risk score from market data for the given `leaders`
    (typically the book's big momentum names)."""
    cfg = cfg or conf.UNWIND
    lb = cfg["lookback"]

    # momentum factor vs market
    mtum_minus_spy = excess_return(market, conf.MOMENTUM_FACTOR, conf.BENCH, lb)

    # extension across leaders
    closes_by = {s: (market.get(s, {}) or {}).get("closes") or [] for s in leaders}
    have = [s for s in leaders if len(closes_by[s]) >= 60]
    frac_extended = (sum(is_extended(closes_by[s], cfg) for s in have) / len(have)
                     if have else 0.0)

    # crowding: avg pairwise correlation of leader daily returns over lookback
    rets = []
    for s in have:
        r = rg.daily_returns(closes_by[s])
        if len(r) >= lb:
            rets.append(r[-lb:])
    avg_corr = avg_pairwise_corr(rets) if len(rets) >= 2 else None

    # breadth divergence: market up over ~20d but <half the leaders above 50DMA
    spy_ret20 = rg.total_return((market.get(conf.BENCH, {}) or {}).get("closes") or [], 20)
    above50 = []
    for s in have:
        ind = rg.indicators({"closes": closes_by[s]})
        if ind["last"] is not None and ind["sma50"] is not None:
            above50.append(ind["last"] > ind["sma50"])
    breadth_pct = (sum(above50) / len(above50)) if above50 else None
    breadth_divergence = bool(spy_ret20 is not None and spy_ret20 > 0
                              and breadth_pct is not None and breadth_pct < 0.5)

    res = unwind_risk_score(
        mtum_minus_spy=mtum_minus_spy, frac_extended=frac_extended,
        avg_corr=avg_corr, vix=vix, breadth_divergence=breadth_divergence, cfg=cfg)
    res["inputs"] = {
        "mtum_minus_spy": mtum_minus_spy, "frac_extended": round(frac_extended, 2),
        "avg_corr": round(avg_corr, 3) if avg_corr is not None else None,
        "vix": vix, "breadth_pct": round(breadth_pct, 2) if breadth_pct is not None else None,
        "breadth_divergence": breadth_divergence,
    }
    return res


def main():
    import json
    syms = list({conf.BENCH, conf.MOMENTUM_FACTOR, *conf.SECTOR_ETFS, *conf.HOLDINGS})
    market = rg.fetch_market(syms)
    vix = rg.fetch_vix()
    leaders = [s for s in conf.HOLDINGS if conf.is_momentum_bet(s)]
    print(json.dumps({"sectors": sector_read(market),
                      "unwind": unwind_read(market, leaders, vix=vix)},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
