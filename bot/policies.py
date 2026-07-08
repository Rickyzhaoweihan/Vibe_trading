#!/usr/bin/env python3
"""Tier 1 — the policy library.

Each policy is a PURE function mapping (regime, market data, current positions,
params) -> a list of trade *intents*. No LLM, no network, no state writes —
fully unit-testable with synthetic price series, exactly like guardrails.

An intent is a plain dict:
  {ticker, side, policy_id, sleeve, hold_class, target_frac, stop_pct,
   realized_vol, reason}
`target_frac` is the policy's conviction as a fraction of equity BEFORE the
brain's per-policy weight / aggressiveness multiplier and BEFORE guardrails'
vol-target scaling and risk caps. Policies decide *what* and *how convinced*;
guardrails decides *how much is actually allowed*.
"""

from __future__ import annotations

import regime as rg

# Leveraged-ETF rotation universe (long 3x + inverse pair per sector).
LONG_LEV = ["TQQQ", "SOXL", "TECL", "UPRO", "FNGU"]
INVERSE = {"TQQQ": "SQQQ", "SOXL": "SOXS", "TECL": "TECS",
           "UPRO": "SPXU", "FNGU": "FNGD"}
ALL_LEV = set(LONG_LEV) | set(INVERSE.values())

SLEEVE = "aggressive"
HOLD_CLASS = "swing_lev"


def _ind(market, sym):
    bars = market.get(sym)
    return rg.indicators(bars) if bars else None


def _intent(ticker, side, policy_id, *, target_frac=0.0, stop_pct=0.06,
            realized_vol=None, reason=""):
    return {
        "ticker": ticker,
        "side": side,
        "policy_id": policy_id,
        "sleeve": SLEEVE,
        "hold_class": HOLD_CLASS,
        "target_frac": round(float(target_frac), 4),
        "stop_pct": float(stop_pct),
        "realized_vol": realized_vol,
        "reason": reason,
    }


def _held_lev(positions, policy_id=None):
    """Leveraged symbols we currently hold (optionally only those a given
    policy opened)."""
    out = []
    for sym, rec in (positions or {}).items():
        if sym not in ALL_LEV:
            continue
        if policy_id and (rec or {}).get("policy_id") != policy_id:
            continue
        out.append(sym)
    return out


# ---- P1: 200-SMA leverage trend rotation -------------------------------

def sma200_trend(regime_out, market, positions, params):
    """Hold the strongest long 3x ETF while the index is in a confirmed
    uptrend (QQQ > 200SMA + golden cross); exit to cash (or a small inverse)
    when the trend breaks. This is the risk filter that avoids the worst
    leverage-decay drawdowns."""
    pid = "sma200_trend"
    tf = float(params.get("target_frac", 0.25))
    stop = float(params.get("stop_pct", 0.06))
    feats = regime_out["features"]
    held = _held_lev(positions, pid)
    intents = []

    uptrend = feats.get("above_200sma") and feats.get("golden_cross")
    if uptrend:
        # rank long-lev candidates by 20-day relative strength
        ranked = []
        for sym in LONG_LEV:
            ind = _ind(market, sym)
            if ind and ind.get("ret_20") is not None:
                ranked.append((ind["ret_20"], sym, ind))
        if ranked:
            ranked.sort(reverse=True)
            _, best, bind = ranked[0]
            # rotate out of any held long that isn't the leader
            for sym in held:
                if sym != best:
                    intents.append(_intent(sym, "sell", pid,
                                            reason="rotate out of laggard"))
            if best not in held:
                intents.append(_intent(best, "buy", pid, target_frac=tf,
                                       stop_pct=stop, realized_vol=bind.get("realized_vol"),
                                       reason="uptrend leader by 20d RS"))
    else:
        # risk-off: exit all long-lev holdings
        for sym in held:
            intents.append(_intent(sym, "sell", pid, reason="QQQ<200SMA, exit to cash"))
        # optional small inverse when momentum is clearly negative
        if params.get("allow_inverse") and (feats.get("momentum_63d") or 0) < 0:
            inv = INVERSE["TQQQ"]
            ind = _ind(market, inv)
            intents.append(_intent(inv, "buy", pid,
                                   target_frac=tf * 0.5, stop_pct=stop,
                                   realized_vol=(ind or {}).get("realized_vol"),
                                   reason="downtrend hedge"))
    return intents


# ---- P2: Connors RSI-2 mean reversion ----------------------------------

def rsi2_meanrev(regime_out, market, positions, params):
    """Buy short-term oversold dips in an uptrend; exit on the bounce. Held
    overnight (swing_lev) so it is NOT a PDT day-trade. Highest win-rate,
    shortest hold — the main P&L engine in trending-but-choppy tape."""
    pid = "rsi2_meanrev"
    sym = params.get("symbol", "TQQQ")
    tf = float(params.get("target_frac", 0.20))
    stop = float(params.get("stop_pct", 0.06))
    oversold = float(params.get("oversold", 10.0))
    overbought = float(params.get("overbought", 70.0))
    feats = regime_out["features"]
    ind = _ind(market, sym)
    if not ind or ind.get("rsi2") is None:
        return []

    held = sym in (positions or {})
    rsi2, last, sma5, prev_high = (ind["rsi2"], ind["last"], ind["sma5"],
                                   ind.get("prev_high"))
    intents = []

    # exit first: the bounce is done — RSI recovered, closed back above the
    # 5-day SMA, or above the prior day's high (classic Connors exits).
    if held:
        bounced = (rsi2 >= overbought
                   or (sma5 is not None and last is not None and last > sma5)
                   or (prev_high and last and last > prev_high))
        if bounced:
            intents.append(_intent(sym, "sell", pid, reason=f"RSI2 {rsi2:.0f} bounce/exit"))
        return intents

    # entry: buy the oversold dip while the broad trend is up. Price is BELOW
    # the short MA during a dip — that's the point — so gate on the long trend.
    if feats.get("above_200sma") and rsi2 <= oversold:
        intents.append(_intent(sym, "buy", pid, target_frac=tf, stop_pct=stop,
                               realized_vol=ind.get("realized_vol"),
                               reason=f"RSI2 {rsi2:.0f} oversold dip in uptrend"))
    return intents


# ---- P3: dual (relative + absolute) momentum rotation ------------------

def dual_momentum(regime_out, market, positions, params):
    """Hold the strongest long-leveraged ETF by 3-month return, but only while
    its absolute momentum is positive (else stay in cash). Classic dual
    momentum: relative strength picks the winner, absolute momentum is the
    risk-off switch."""
    pid = "dual_momentum"
    tf = float(params.get("target_frac", 0.30))
    stop = float(params.get("stop_pct", 0.08))
    held = _held_lev(positions, pid)
    intents = []

    ranked = []
    for sym in LONG_LEV:
        ind = _ind(market, sym)
        if ind and ind.get("ret_63") is not None:
            ranked.append((ind["ret_63"], sym, ind))
    if not ranked:
        return []
    ranked.sort(reverse=True)
    best_ret, best, bind = ranked[0]

    if best_ret is not None and best_ret > 0:
        for sym in held:
            if sym != best:
                intents.append(_intent(sym, "sell", pid, reason="momentum rotation"))
        if best not in held:
            intents.append(_intent(best, "buy", pid, target_frac=tf, stop_pct=stop,
                                   realized_vol=bind.get("realized_vol"),
                                   reason=f"top 3-mo momentum {best_ret:+.1%}"))
    else:
        # absolute momentum negative -> exit to cash
        for sym in held:
            intents.append(_intent(sym, "sell", pid, reason="absolute momentum negative"))
    return intents


POLICIES = {
    "sma200_trend": sma200_trend,
    "rsi2_meanrev": rsi2_meanrev,
    "dual_momentum": dual_momentum,
}


# ---- orchestrator: apply brain routing, merge conflicting intents ------

def _merge(intents):
    """Resolve conflicts across policies for the same ticker: a sell always
    wins over a buy (risk-off bias); among buys keep the highest conviction;
    among sells keep the first."""
    sells, buys = {}, {}
    for it in intents:
        if it["side"] == "sell":
            sells.setdefault(it["ticker"], it)
        else:
            cur = buys.get(it["ticker"])
            if cur is None or it["target_frac"] > cur["target_frac"]:
                buys[it["ticker"]] = it
    out = list(sells.values())
    out += [it for t, it in buys.items() if t not in sells]
    return out


def evaluate(routing, regime_out, market, positions, policies_cfg):
    """Run the brain-selected active policies, scale buy convictions by each
    policy's weight and the global aggressiveness, and merge conflicts.

    `routing` = {active_policies:[...], weights:{pid:w}, aggressiveness:float}.
    `policies_cfg` = the policies.json map (per-policy enabled/weight/params).
    """
    active = routing.get("active_policies") or list(POLICIES)
    weights = routing.get("weights") or {}
    aggression = float(routing.get("aggressiveness", 1.0))
    out = []
    for pid in active:
        fn = POLICIES.get(pid)
        if not fn:
            continue
        cfg = (policies_cfg or {}).get(pid, {})
        if not cfg.get("enabled", True):
            continue
        raw = fn(regime_out, market, positions, cfg.get("params", {}))
        w = float(weights.get(pid, cfg.get("weight", 1.0)))
        for it in raw:
            if it["side"] == "buy":
                it["target_frac"] = round(it["target_frac"] * w * aggression, 4)
            out.append(it)
    return _merge(out)
