#!/usr/bin/env python3
"""Tier 2 — the brain.

ONE cheap structured LLM call per routing decision. Given the market regime,
each policy's recent performance, the remaining day-trade budget and current
positions, it picks which policies are ACTIVE today, their WEIGHTS, and a
global AGGRESSIVENESS multiplier. It routes between proven policies — it never
names a ticker and never authors an order (guardrails does that).

`default_route` is a pure, deterministic regime->policy mapping used both as
the fallback when the LLM call fails and as the safety floor. The LLM only
ever refines this; if anything is off, we fall back to it.
"""

from __future__ import annotations

import json
import os

import regime as rg
import policies as pol

MODEL = os.environ.get("BRAIN_MODEL", "claude-sonnet-4-6")
KNOWN_POLICIES = list(pol.POLICIES)


def _enabled(policies_cfg, pid):
    return (policies_cfg or {}).get(pid, {}).get("enabled", True)


def default_route(features, policies_cfg=None):
    """Deterministic regime -> {active_policies, weights, aggressiveness}.

    Conservative by construction: trend-following is always on as the risk
    filter; mean-reversion is added in constructive regimes; aggressiveness is
    dialed down in high-vol / risk-off tape."""
    label = features.get("label") if isinstance(features, dict) and "label" in features else None
    # accept either a full regime_out or its features dict
    feats = features.get("features", features) if isinstance(features, dict) else {}
    label = label or _infer_label(feats)

    if label == rg.RISK_ON_TREND:
        active, weights, aggr = ["sma200_trend", "rsi2_meanrev"], {"sma200_trend": 1.0, "rsi2_meanrev": 1.0}, 1.0
    elif label == rg.NEUTRAL:
        active, weights, aggr = ["sma200_trend", "rsi2_meanrev"], {"sma200_trend": 0.7, "rsi2_meanrev": 0.6}, 0.6
    elif label == rg.HIGH_VOL_CHOP:
        active, weights, aggr = ["rsi2_meanrev"], {"rsi2_meanrev": 0.5}, 0.4
    else:  # RISK_OFF_TREND
        active, weights, aggr = ["sma200_trend"], {"sma200_trend": 0.6}, 0.5

    active = [p for p in active if p in KNOWN_POLICIES and _enabled(policies_cfg, p)]
    weights = {p: w for p, w in weights.items() if p in active}
    return {"active_policies": active, "weights": weights, "aggressiveness": aggr,
            "source": "default", "regime": label}


def _infer_label(feats):
    if feats.get("high_vol"):
        return rg.HIGH_VOL_CHOP
    if feats.get("above_200sma") and feats.get("golden_cross"):
        return rg.RISK_ON_TREND
    if feats.get("above_200sma") is False:
        return rg.RISK_OFF_TREND
    return rg.NEUTRAL


def sanitize(routing, policies_cfg=None, fallback=None):
    """Coerce an arbitrary routing object into a valid one: drop unknown or
    disabled policies, clamp aggressiveness, default weights. Returns the
    fallback if nothing valid survives."""
    fb = fallback or {"active_policies": [], "weights": {}, "aggressiveness": 0.0}
    if not isinstance(routing, dict):
        return fb
    active = [p for p in (routing.get("active_policies") or [])
              if p in KNOWN_POLICIES and _enabled(policies_cfg, p)]
    if not active:
        return fb
    weights = routing.get("weights") or {}
    weights = {p: max(0.0, min(2.0, float(weights.get(p, 1.0)))) for p in active}
    try:
        aggr = max(0.0, min(1.5, float(routing.get("aggressiveness", 1.0))))
    except (TypeError, ValueError):
        aggr = 1.0
    return {"active_policies": active, "weights": weights, "aggressiveness": aggr,
            "source": routing.get("source", "llm"),
            "regime": routing.get("regime")}


# ---- the LLM routing call (best-effort, falls back to default) ----------

ROUTING_TOOL = {
    "name": "route",
    "description": "Select which trading policies are active today, their weights, and aggressiveness.",
    "input_schema": {
        "type": "object",
        "properties": {
            "active_policies": {"type": "array", "items": {"type": "string", "enum": KNOWN_POLICIES}},
            "weights": {"type": "object", "additionalProperties": {"type": "number"}},
            "aggressiveness": {"type": "number", "minimum": 0, "maximum": 1.5},
            "rationale": {"type": "string"},
        },
        "required": ["active_policies", "weights", "aggressiveness"],
    },
}


def _prompt(regime_out, policy_perf, dt_budget, positions, news="", memory=""):
    extra = ""
    if news:
        extra += f"\n\nMarket context (news/sentiment):\n{news}"
    if memory:
        extra += f"\n\nLessons from past decisions:\n{memory}"
    return (
        "You route a leveraged-ETF trading bot between proven systematic policies. "
        "You do NOT pick tickers or sizes — only which policies run, their relative "
        "weights, and a global aggressiveness multiplier.\n\n"
        f"Available policies: {KNOWN_POLICIES}\n"
        "- sma200_trend: trend-follow the strongest 3x ETF while QQQ>200SMA; the risk filter.\n"
        "- rsi2_meanrev: buy oversold dips in an uptrend, exit on the bounce; short hold.\n"
        "- dual_momentum: hold the strongest 3x ETF by 3-mo momentum while it's positive.\n\n"
        f"Current market regime: {regime_out.get('label')}\n"
        f"Regime features: {json.dumps(regime_out.get('features', {}), default=str)}\n"
        f"Per-policy trailing performance: {json.dumps(policy_perf, default=str)}\n"
        f"Remaining same-day day-trade budget: {dt_budget}\n"
        f"Open positions: {list((positions or {}).keys())}"
        f"{extra}\n\n"
        "Favor the trend filter in clean uptrends, lean on mean-reversion in choppy "
        "uptrends, and cut aggressiveness in high-vol or risk-off regimes. Let the news/"
        "sentiment context and past lessons adjust aggressiveness (e.g. de-risk into major "
        "macro events). Down-weight policies whose trailing performance is poor. Call the "
        "route tool."
    )


def route(regime_out, policy_perf=None, dt_budget=0, positions=None, *,
          news="", memory="", model=None):
    """Best-effort LLM routing with a deterministic fallback. Never raises.
    `news` (macro/sentiment digest) and `memory` (past-decision lessons) add
    context but are optional — routing works on regime alone if both empty."""
    fb = default_route(regime_out)
    try:
        import anthropic
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        resp = client.messages.create(
            model=model or MODEL,
            max_tokens=400,
            tools=[ROUTING_TOOL],
            tool_choice={"type": "tool", "name": "route"},
            messages=[{"role": "user",
                       "content": _prompt(regime_out, policy_perf or {}, dt_budget,
                                          positions, news, memory)}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "route":
                out = sanitize(block.input, None, fallback=fb)
                out["regime"] = regime_out.get("label")
                return out
        return fb
    except Exception as e:
        fb["error"] = str(e)
        return fb
