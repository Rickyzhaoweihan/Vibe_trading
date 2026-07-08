#!/usr/bin/env python3
"""L1 — Macro strategy / regime.

Reads the cross-asset panel (rates, dollar, gold, oil, VIX, BTC, global indices,
futures), classifies the regime (reusing regime.compute_regime), and — the part
that makes it a *strategy* rather than a backdrop — emits a net-equity-exposure
recommendation and an ETF hedge/expression list keyed to the regime.

Pure functions (rate_direction, recommend_exposure) are unit-tested offline;
macro_read() is the thin networked orchestrator.
"""

from __future__ import annotations

import sys
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import regime as rg


# ---- pure helpers -------------------------------------------------------

def rate_direction(closes, *, lookback=10, thresh=0.02):
    """'rising' / 'falling' / 'flat' for a yield series (e.g. ^TNX) over the
    last `lookback` bars, using a relative-change threshold so it is robust to
    how the vendor scales the quote."""
    if not closes or len(closes) < lookback + 1:
        return "flat"
    prev = closes[-1 - lookback]
    last = closes[-1]
    if not prev:
        return "flat"
    chg = (last - prev) / abs(prev)
    if chg > thresh:
        return "rising"
    if chg < -thresh:
        return "falling"
    return "flat"


def recommend_exposure(label, *, rates_rising=False, high_vol=False):
    """Map a regime (+ rate direction + vol) to a net-exposure target and a
    hedge/expression list. Deterministic and pure.

    Returns {net_target, hedges:[keys], expressions:{key:[tickers]}, notes:[...]}.
    """
    base = conf.EXPOSURE_BY_REGIME.get(label, 0.60)
    hedges = []
    notes = []

    if rates_rising:
        base = round(max(0.20, base - 0.15), 2)
        notes.append("10Y rising — trim long-duration growth; raise cash before adding semis/AI.")
        hedges.append("cash")
    elif label in (rg.RISK_ON_TREND, rg.NEUTRAL):
        notes.append("Rates stable/easing — supportive backdrop for growth & duration (TLT/IEF).")

    if label == rg.RISK_OFF_TREND:
        hedges += ["duration", "gold", "inverse", "cash"]
        notes.append("Risk-off trend — defensive posture; hold hedges, don't chase bounces.")
    elif label == rg.HIGH_VOL_CHOP:
        hedges += ["gold", "defensive", "cash"]
        notes.append("High-vol chop — cut gross, keep dry powder, fade extremes rather than trend.")
    elif label == rg.RISK_ON_TREND:
        notes.append("Risk-on trend — momentum favored; stay invested but respect the unwind score.")

    if high_vol and "cash" not in hedges:
        hedges.append("cash")

    seen = set()
    hedges = [h for h in hedges if not (h in seen or seen.add(h))]
    expressions = {h: conf.HEDGES[h] for h in hedges if h in conf.HEDGES}
    return {"net_target": base, "hedges": hedges, "expressions": expressions, "notes": notes}


def _pct_vs_prev(ind):
    last, prev = ind.get("last"), ind.get("prev_close")
    if last is None or not prev:
        return None
    return last / prev - 1.0


def _trend(ind):
    last, s50 = ind.get("last"), ind.get("sma50")
    if last is None or s50 is None:
        return "n/a"
    return "up" if last > s50 else "down"


def panel_snapshot(market):
    """A readable cross-asset snapshot: name -> {symbol, last, pct, trend}."""
    out = {}
    items = list(conf.MACRO_PANEL.items())
    items += [(f"idx:{s}", s) for s in conf.GLOBAL_INDICES]
    items += [(f"fut:{s}", s) for s in conf.FUTURES]
    for name, sym in items:
        ind = rg.indicators(market.get(sym, {}))
        out[name] = {"symbol": sym, "last": ind.get("last"),
                     "pct": _pct_vs_prev(ind), "trend": _trend(ind)}
    return out


def build_narrative(label, rates_dir, snap):
    """One-paragraph macro read for the desk note."""
    def fmt(name):
        d = snap.get(name) or {}
        p = d.get("pct")
        return f"{name} {('%+.1f%%' % (p*100)) if p is not None else 'n/a'}"
    bits = [fmt(k) for k in ("rates_10y", "dollar", "gold", "oil", "vix", "btc")]
    return (f"Regime: {label}. Rates {rates_dir}. "
            + ", ".join(bits) + ".")


# ---- networked orchestrator --------------------------------------------

def macro_read(market=None, vix=None):
    """Full L1 read. Fetches the panel if `market` not supplied."""
    syms = list({conf.BENCH, "QQQ", conf.MOMENTUM_FACTOR,
                 *conf.MACRO_PANEL.values(), *conf.GLOBAL_INDICES, *conf.FUTURES})
    if market is None:
        market = rg.fetch_market(syms)
    if vix is None:
        vix = rg.fetch_vix()
    reg = rg.compute_regime(market, index="QQQ", vix=vix)
    label = reg["label"]
    tnx = (market.get(conf.MACRO_PANEL["rates_10y"], {}) or {}).get("closes") or []
    rdir = rate_direction(tnx)
    exposure = recommend_exposure(
        label, rates_rising=(rdir == "rising"),
        high_vol=bool(reg["features"].get("high_vol")))
    snap = panel_snapshot(market)
    return {
        "label": label,
        "regime": reg,
        "rates_direction": rdir,
        "panel": snap,
        "exposure": exposure,
        "narrative": build_narrative(label, rdir, snap),
        "vix": vix,
        "_market": market,   # passed downstream so L2/L5 reuse the same fetch
    }


def main():
    import json
    r = macro_read()
    r.pop("_market", None)
    r.pop("regime", None)
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
