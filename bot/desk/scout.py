#!/usr/bin/env python3
"""L3 — Idea scout.

Screens a liquid candidate pool (plus crypto majors) for swing/position ideas
the user does NOT already own, ranking by momentum/relative strength and trend.
Includes mean-reversion (oversold-in-uptrend) flags, not only breakouts. The
top names are handed to L4 deep research; the rest are context.

Pure ranking functions are unit-tested; scout() does the fetch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import regime as rg


def market_discovery(*, screens=None, count=None, timeout=20):
    """Best-effort LIVE market screen (yfinance predefined screens) → a list of
    fresh, liquid symbols beyond the curated pool. Empty list on any failure so
    the scout still runs off the curated pool + universe.json."""
    if not conf.SCOUT.get("discover"):
        return []
    screens = screens or conf.SCOUT["screens"]
    count = count or conf.SCOUT["screen_count"]
    out, seen = [], set()
    try:
        import yfinance as yf
    except Exception:
        return []
    for name in screens:
        try:
            r = yf.screen(name, count=count)
            for q in (r.get("quotes", []) if isinstance(r, dict) else []):
                sym = (q.get("symbol") or "").strip()
                price = q.get("regularMarketPrice") or 0
                # equities only (skip ETFs/indices/crypto); price filter for junk
                if (sym and sym not in seen and "-" not in sym and "." not in sym
                        and (not price or price >= conf.SCOUT["min_price"])):
                    seen.add(sym); out.append(sym)
        except Exception:
            continue
    return out


def _candidate_pool():
    """Scout pool = curated conf.SCOUT_POOL + universe.json + a live market screen."""
    pool = list(conf.SCOUT_POOL)
    try:
        u = json.loads((BOT_DIR / "universe.json").read_text())
        pool += u.get("tickers", []) + u.get("core_watchlist", [])
    except Exception:
        pass
    pool += market_discovery()
    seen = set()
    return [s for s in pool if not (s in seen or seen.add(s))]


def score_candidate(closes, bench_closes, *, lookback=63):
    """Rank score for one name — tuned to prefer a GOOD ENTRY over a blow-off chase
    (the momentum-only rank was buying extended names that then reverted, −12% avg).

    Rewards relative strength + an intact uptrend, gives the biggest bonus to an
    oversold dip in an uptrend (reversion), and PENALIZES over-extension above the
    50DMA. Returns None on thin data, else {score, excess, uptrend, setup, ext, entry}
    where setup is 'momentum'|'reversion' and entry is dip|near-support|trend|extended.
    """
    er_s = rg.total_return(closes, lookback)
    er_b = rg.total_return(bench_closes, lookback)
    if er_s is None or er_b is None:
        return None
    ind = rg.indicators({"closes": closes})
    last, s50, s200, r2 = ind["last"], ind["sma50"], ind["sma200"], ind["rsi2"]
    uptrend = bool(last is not None and s200 is not None and last > s200)
    excess = er_s - er_b
    oversold = bool(r2 is not None and r2 <= 10.0)
    ext = (last - s50) / s50 if (last and s50) else 0.0        # extension above 50DMA
    setup = "reversion" if (uptrend and oversold) else "momentum"

    # Relative strength matters, but CAP its contribution (diminishing returns past
    # +50% excess) so a parabolic blow-off can't dominate the rank on raw momentum
    # alone — entry quality below then decides the order.
    score = 0.5 * max(-0.30, min(excess, 0.50))
    score += 0.03 if uptrend else -0.08
    if setup == "reversion":
        score += 0.10                                          # oversold dip in uptrend = best entry
    if ext > 0.15:
        score -= (ext - 0.15) * 1.5                            # heavily fade a blow-off (the loss driver)
    elif ext > 0.10:
        score -= (ext - 0.10) * 0.7
    elif 0.0 <= ext <= 0.06 and uptrend:
        score += 0.04                                          # near rising support = clean entry
    entry = ("dip" if setup == "reversion"
             else "extended" if ext > 0.12
             else "near-support" if (uptrend and ext <= 0.06)
             else "trend")
    return {"score": round(score, 4), "excess": round(excess, 4), "uptrend": uptrend,
            "setup": setup, "ext": round(ext, 4), "entry": entry}


def rank_pool(market, pool, bench, *, lookback=63, exclude=()):
    ranked = []
    exclude = set(exclude)
    for s in pool:
        if s in exclude:
            continue
        sc = score_candidate((market.get(s, {}) or {}).get("closes") or [],
                             (market.get(bench, {}) or {}).get("closes") or [],
                             lookback=lookback)
        if sc:
            ranked.append({"ticker": s, **sc})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def scout(*, held=None, market=None, top_n=None, lookback=63):
    """Return {equity: [...top ideas not held...], crypto: [...]}.

    Tags each idea with its cluster + a `diversifier` flag (outside the user's
    crowded AI-semis complex) and applies a mild `crowded_penalty` so a diversifying
    idea edges out an equally-strong crowded one — the book is already ~60%+ one
    factor, so a NEW idea in that same factor barely helps."""
    top_n = top_n or conf.SCOUT.get("top_n", 8)
    held = set(held if held is not None else conf.HOLDINGS)
    pool = _candidate_pool()
    syms = list({conf.BENCH, *pool, *conf.CRYPTO_MAJORS})
    if market is None:
        market = rg.fetch_market(syms)
    equity = rank_pool(market, pool, conf.BENCH, lookback=lookback, exclude=held)
    pen = conf.SCOUT.get("crowded_penalty", 0.0)
    for r in equity:
        crowded = conf.is_momentum_bet(r["ticker"])
        r["cluster"] = conf.cluster_of(r["ticker"])
        r["diversifier"] = not crowded
        if crowded:
            r["score"] = round(r["score"] - pen, 4)
    equity.sort(key=lambda x: x["score"], reverse=True)
    crypto = rank_pool(market, conf.CRYPTO_MAJORS, conf.BENCH, lookback=lookback,
                       exclude=held)
    # Only surface names that are actually a good SETUP — an intact uptrend or a
    # genuine oversold dip. A name in a downtrend with deeply negative relative
    # strength (e.g. crypto −40% vs SPY) is not an "idea"; don't recommend it.
    worthy = lambda r: r.get("uptrend") or r.get("setup") == "reversion"
    equity = [r for r in equity if worthy(r)]
    crypto = [r for r in crypto if worthy(r)]
    return {"equity": equity[:top_n], "crypto": crypto[:top_n],
            "equity_all_ranked": equity}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    res = scout(top_n=args.top)
    res.pop("equity_all_ranked", None)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
