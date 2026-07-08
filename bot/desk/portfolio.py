#!/usr/bin/env python3
"""L5 — Portfolio / risk.

Looks at the actual book: per-cluster/factor exposure, concentration, how much
of the book is the single crowded momentum bet, and — driven by the L1 exposure
target and the L2 unwind score — the defensive actions to take (trim winners,
raise cash, tighten stops, apply hedges).

Pure functions (values/clusters/concentration/crowding/defensive_actions) are
unit-tested; portfolio_read() is the thin orchestrator that prices the book.
"""

from __future__ import annotations

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


def load_positions(path=None):
    path = Path(path) if path else conf.POSITIONS_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        # The live snapshot is missing/unreadable: return a tagged offline seed so
        # health.check_positions can flag it LOUDLY rather than the desk silently
        # analyzing a book that isn't the user's.
        return {"source": "seed-fallback", "as_of": None,
                "positions": [{"symbol": s, "quantity": 0.0} for s in conf.HOLDINGS],
                "crypto_value": 0.0}


# ---- detect the user's own trades (diff two snapshots) ------------------

def diff_positions(prev, curr, *, min_delta=1e-4):
    """Detect trades the user made by diffing two book snapshots.

    Returns a list of {symbol, kind, prev_qty, curr_qty, delta}, kind being
    NEW / CLOSED / ADDED / REDUCED. This is how the desk says 'your trade has
    been monitored' and re-analyzes against the book you actually hold now. Pure.
    """
    def qmap(doc):
        return {p.get("symbol"): float(p.get("quantity") or 0.0)
                for p in (doc or {}).get("positions", []) if p.get("symbol")}
    a, b = qmap(prev), qmap(curr)
    out = []
    for sym in sorted(set(a) | set(b)):
        pa, pb = a.get(sym, 0.0), b.get(sym, 0.0)
        d = pb - pa
        if abs(d) < min_delta:
            continue
        if pa <= min_delta:
            kind = "NEW"
        elif pb <= min_delta:
            kind = "CLOSED"
        elif d > 0:
            kind = "ADDED"
        else:
            kind = "REDUCED"
        out.append({"symbol": sym, "kind": kind, "prev_qty": round(pa, 6),
                    "curr_qty": round(pb, 6), "delta": round(d, 6)})
    return out


# ---- pure analytics -----------------------------------------------------

def position_values(positions, prices):
    """{symbol: market_value} from positions [{symbol, quantity}] and a
    {symbol: price} map. Symbols without a price are skipped."""
    out = {}
    for p in positions:
        sym, qty = p.get("symbol"), p.get("quantity") or 0.0
        px = prices.get(sym)
        if px:
            out[sym] = qty * px
    return out


def cluster_exposure(values, *, crypto_value=0.0):
    """{cluster: {value, pct}} over the whole book (crypto folded in)."""
    buckets = {}
    for sym, v in values.items():
        buckets[conf.cluster_of(sym)] = buckets.get(conf.cluster_of(sym), 0.0) + v
    if crypto_value:
        buckets["crypto"] = buckets.get("crypto", 0.0) + crypto_value
    total = sum(buckets.values()) or 1.0
    return {c: {"value": round(v, 2), "pct": round(v / total, 4)}
            for c, v in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)}


def concentration(values, *, crypto_value=0.0):
    """HHI + top-1 / top-3 weights over individual positions."""
    vals = dict(values)
    if crypto_value:
        vals["__crypto__"] = crypto_value
    total = sum(vals.values()) or 1.0
    weights = sorted((v / total for v in vals.values()), reverse=True)
    hhi = sum(w * w for w in weights)
    return {
        "hhi": round(hhi, 4),
        "top1": round(weights[0], 4) if weights else 0.0,
        "top3": round(sum(weights[:3]), 4),
        "positions": len(weights),
    }


def crowding_share(values, *, crypto_value=0.0):
    """Fraction of the book sitting in the crowded momentum/AI-semis complex."""
    total = (sum(values.values()) + crypto_value) or 1.0
    mom = sum(v for s, v in values.items() if conf.is_momentum_bet(s))
    return round(mom / total, 4)


def plan_trades(calls, *, values, total, net_target, current_net, band, prices=None,
                cash=None):
    """Annotate each actionable call with a dollar amount, share count, and a
    when, in place — sized to be DIRECTLY placeable, not aspirational.

    Sizing: SELL = full position; TRIM = 25% (33% if unwind elevated/high) of the
    position; NEW_BUY = 5% of book; BUY add = 3% of book — but every buy is capped
    by the cash actually available (`cash`, drawn down in call order) so the plan
    never tells the user to spend money they don't have. Sells/trims get the share
    count from the position value; buys get an approximate share count at the
    entry zone (fractional, Robinhood supports it). Timing: risk-reducing trades
    go 'now' when unwind is HIGH else at the open into strength; buys scale in on
    a dip. Returns the total cash to raise toward the net-exposure target.
    """
    prices = prices or {}
    urgent = band == "high"
    cash_to_raise = max(0.0, (current_net - net_target) * total)
    trim_frac = 0.33 if band in ("elevated", "high") else 0.25
    budget = None if cash is None else max(0.0, float(cash))
    _conv = {"high": 3, "medium": 2, "low": 1}

    # exits first (they RAISE cash and don't draw the budget)
    for c in calls:
        a, sym = c.get("action"), c.get("ticker")
        v, last = values.get(sym, 0.0), prices.get(sym)
        c.setdefault("dollars", None); c.setdefault("when", None); c.setdefault("shares", None)
        if a == "SELL":
            c["dollars"] = round(v)
            if last:
                c["shares"] = round(v / last, 4)
            c["when"] = "now — market" if urgent else "at the open"
        elif a == "TRIM":
            c["dollars"] = round(v * trim_frac)
            if last:
                c["shares"] = round(v * trim_frac / last, 4)
            c["when"] = "now — into strength" if urgent else "at the open, into strength"

    # buys drain the cash budget in CONVICTION order (highest first; a BUY-add to a
    # held winner outranks a NEW_BUY chase at equal conviction) — not list order,
    # so the best idea gets funded first rather than whoever appears earliest.
    buys = [c for c in calls if c.get("action") in ("BUY", "NEW_BUY")]
    buys.sort(key=lambda c: (_conv.get(c.get("conviction"), 0),
                             1 if c.get("action") == "BUY" else 0), reverse=True)
    for c in buys:
        sym, last = c.get("ticker"), prices.get(c.get("ticker"))
        want = (0.05 if c["action"] == "NEW_BUY" else 0.03) * total
        if budget is not None:
            want = min(want, budget)
            budget = max(0.0, budget - want)
        c["dollars"] = round(want)
        if c["dollars"] < 5:               # broker minimum — an unfundable call is noise, not a KEEP
            c["action"] = "KEEP"
            c["reason"] = (c.get("reason") or "") + " [现金不足，本次跳过买入]"
            c["dollars"] = None
            continue
        if last:
            # a chase-prone idea (extended) only gets bought on a deeper pullback
            dip = 0.95 if c.get("extended") else 0.97
            zone = round(last * dip, 2)
            c["entry_zone"] = zone
            c["shares"] = round(c["dollars"] / zone, 4)
            c["when"] = (f"仅回调至 ${zone:,.2f} 买入（追高风险）" if c.get("extended")
                         else f"scale in on a dip toward ${zone:,.2f}")
        else:
            c["when"] = "scale in over 1–2 days"
    return round(cash_to_raise)


def needs_sqqq_confirm(regime_label, unwind_band):
    """True when the tape is deteriorating enough to actually CONSIDER the -3x
    SQQQ, so the caller spends a deep-research confirm on the underlying. Fires at
    ELEVATED unwind (not just high) so the check happens early — a hedge you only
    consider after the regime is fully risk-off is usually too late."""
    return (regime_label in ("RISK_OFF_TREND", "HIGH_VOL_CHOP")
            or unwind_band in ("elevated", "high"))


def sqqq_decision(regime_label, unwind_band, confirm_rating):
    """Graded, reasonable call on whether to LEAD the hedge with the -3x SQQQ.

    Weighs the risk signal (regime + unwind) against the underlying's deep-research
    view. The bar is sensible, not maximal: you don't need an outright Sell — a
    cautious read in a clearly deteriorating tape is enough. But you never -3x-short
    a tape the research still rates a Buy, and a missing/failed confirm won't deploy
    leverage on its own. Returns (use_tactical: bool, note: str).
    """
    strong = regime_label in ("RISK_OFF_TREND", "HIGH_VOL_CHOP") or unwind_band == "high"
    mild = unwind_band == "elevated"
    bullish = confirm_rating in ("Buy", "Overweight")
    bearish = confirm_rating in ("Sell", "Underweight")
    neutral = confirm_rating == "Hold"

    if confirm_rating is None:
        return False, (f"-3x SQQQ would fit the tape, but the deep-research confirm on the "
                       f"underlying didn't complete — staying with -1x PSQ until it does.")
    if bullish:
        return False, (f"-3x SQQQ stood down: research still rates the underlying {confirm_rating} "
                       f"— don't -3x-short a tape that's rated a buy; using -1x PSQ.")
    if bearish and (strong or mild):
        return True, (f"-3x SQQQ ON: research rates the underlying {confirm_rating} and the tape is "
                      f"deteriorating — tactical, honor the hard stop, don't hold through chop.")
    if neutral and strong:
        return True, (f"-3x SQQQ ON (tactical): research is neutral but the regime/unwind signal is "
                      f"clearly risk-off — short leash, hard stop, days not weeks.")
    if neutral and mild:
        return False, (f"-3x SQQQ on watch: research neutral and unwind only elevated — not yet; "
                       f"hold the -1x PSQ and escalate if it turns.")
    return False, "Calm-enough tape — -1x PSQ; the -3x stays holstered until risk builds."


def hedge_plan(*, equity, regime_label, unwind_band, current_net, net_target,
               crowding=0.0, base_hedge=None, primary="PSQ", tactical="SQQQ",
               confirm_rating=None):
    """Size a downside hedge from inverse ETFs so the book gains when the market
    falls. Pure.

    Starts at a standing floor (`base_hedge` — the user wants permanent insurance)
    and escalates the hedge NOTIONAL (% of equity to neutralize) as the regime
    turns risk-off, unwind risk rises, the book runs over its net-exposure target,
    or concentration is extreme. Returns the notional plus two ways to express it:
    a -1x holdable instrument (capital ≈ notional) and a -3x tactical one (capital
    ≈ notional/3, but it DECAYS).

    Whether to LEAD with the -3x is delegated to `sqqq_decision` — a graded read of
    the risk signal vs the underlying's deep-research view (reasonable, not maximal).
    """
    base = conf.BASE_HEDGE if base_hedge is None else base_hedge
    bump, reasons = 0.0, []
    if regime_label == "RISK_OFF_TREND":
        bump += 0.25; reasons.append("risk-off trend")
    elif regime_label == "HIGH_VOL_CHOP":
        bump += 0.15; reasons.append("high-vol chop")
    if unwind_band == "high":
        bump += 0.15; reasons.append("unwind risk HIGH")
    elif unwind_band == "elevated":
        bump += 0.08; reasons.append("unwind elevated")
    gap = current_net - net_target
    if gap > 0.05:
        bump += min(0.10, gap); reasons.append(f"{gap*100:.0f}pt over net target")
    if crowding >= 0.60:
        bump += 0.05; reasons.append(f"{crowding*100:.0f}% crowded in one bet")

    target_pct = round(min(0.40, base + bump), 3)
    notional = round(target_pct * equity)
    strong_risk = needs_sqqq_confirm(regime_label, unwind_band)
    use_tactical, confirm_note = sqqq_decision(regime_label, unwind_band, confirm_rating)

    def _opt(tkr):
        spec = conf.HEDGE_INSTRUMENTS.get(tkr, {})
        lev = spec.get("leverage", 1)
        return {"ticker": tkr, "capital": round(notional / lev),
                "neutralizes": notional, "leverage": lev,
                "holdable": spec.get("holdable", True), "note": spec.get("note", "")}

    return {
        "target_pct": target_pct,
        "notional": notional,
        "urgency": "now" if strong_risk else "standing insurance (small in this tape)",
        "recommend": tactical if use_tactical else primary,   # which to lead with
        "confirmed": bool(use_tactical),
        "confirm_rating": confirm_rating,
        "confirm_note": confirm_note,
        "options": [_opt(primary), _opt(tactical)],
        "rationale": ", ".join(reasons) or "baseline insurance only — calm tape, keep it light",
    }


def defensive_actions(*, unwind_band, crowding, net_target, current_net,
                      hedges=None):
    """Concrete portfolio actions from the regime exposure target + unwind band
    + crowding. Returns an ordered list of plain-language directives."""
    actions = []
    gap = current_net - net_target
    if gap > 0.05:
        actions.append(
            f"Reduce net exposure ~{gap*100:.0f} pts: raise cash toward "
            f"{net_target*100:.0f}% invested (currently ~{current_net*100:.0f}%).")
    if unwind_band == "high":
        actions.append("Unwind risk HIGH — trim extended momentum winners into "
                       "strength and tighten stops across the semis/AI cluster.")
    elif unwind_band == "elevated":
        actions.append("Unwind risk ELEVATED — tighten stops on winners; do not "
                       "add to the crowded names.")
    if crowding >= 0.50:
        actions.append(f"Concentration — {crowding*100:.0f}% of the book is one "
                       "momentum/AI-semis bet; diversify or hedge that factor.")
    if hedges:
        names = ", ".join(t for h in hedges for t in conf.HEDGES.get(h, []))
        if names:
            actions.append(f"Macro hedges to consider: {names}.")
    if not actions:
        actions.append("No portfolio action — exposure, concentration and unwind "
                       "risk all within tolerance; hold and follow stops.")
    return actions


# ---- orchestrator -------------------------------------------------------

def portfolio_read(positions_doc, market, *, macro=None, unwind=None):
    prices = {}
    for p in positions_doc.get("positions", []):
        ind = rg.indicators(market.get(p["symbol"], {}))
        if ind["last"] is not None:
            prices[p["symbol"]] = ind["last"]

    crypto_value = positions_doc.get("crypto_value", 0.0) or 0.0
    values = position_values(positions_doc.get("positions", []), prices)
    total = sum(values.values()) + crypto_value
    cash = positions_doc.get("cash", 0.0) or 0.0
    current_net = round(total / (total + cash), 4) if (total + cash) else 1.0

    clusters = cluster_exposure(values, crypto_value=crypto_value)
    conc = concentration(values, crypto_value=crypto_value)
    crowding = crowding_share(values, crypto_value=crypto_value)

    net_target = (macro or {}).get("exposure", {}).get("net_target", 0.70)
    band = (unwind or {}).get("band", "low")
    hedges = (macro or {}).get("exposure", {}).get("hedges", [])
    actions = defensive_actions(unwind_band=band, crowding=crowding,
                                net_target=net_target, current_net=current_net,
                                hedges=hedges)
    return {
        "total_value": round(total, 2),
        "current_net": current_net,
        "clusters": clusters,
        "concentration": conc,
        "crowding_share": crowding,
        "actions": actions,
        "values": {s: round(v, 2) for s, v in values.items()},
    }


def main():
    pos = load_positions()
    syms = [p["symbol"] for p in pos.get("positions", [])]
    market = rg.fetch_market(syms)
    print(json.dumps(portfolio_read(pos, market), indent=2, default=str))


if __name__ == "__main__":
    main()
