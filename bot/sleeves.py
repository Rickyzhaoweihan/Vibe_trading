#!/usr/bin/env python3
"""Sleeve accounting — the 80/20 split between the aggressive leveraged book
and the deep-researched long-term core book.

Pure helpers. guardrails.py uses these to cap each sleeve independently so the
order author (not the brain) enforces the split.
"""

from __future__ import annotations

# Default target fractions of total equity per sleeve.
SLEEVE_FRACS = {"aggressive": 0.80, "core": 0.20}

AGGRESSIVE = "aggressive"
CORE = "core"


def sleeve_of(state_pos_record, default=AGGRESSIVE):
    """The sleeve a held position belongs to, from its recorded tag."""
    s = (state_pos_record or {}).get("sleeve")
    return s if s in SLEEVE_FRACS else default


def sleeve_target_frac(sleeves_cfg, sleeve):
    """Target fraction for a sleeve, honoring an override in state['sleeves']."""
    cfg = (sleeves_cfg or {}).get(sleeve) or {}
    f = cfg.get("target_frac")
    return float(f) if f is not None else SLEEVE_FRACS.get(sleeve, 0.0)


def sleeve_exposure(positions, state_pos, pos_value):
    """Current dollar exposure per sleeve.

    `positions` maps symbol -> live snapshot position; `state_pos` maps symbol
    -> our recorded record (carrying the sleeve tag); `pos_value(sym)` returns
    a symbol's market value.
    """
    out = {AGGRESSIVE: 0.0, CORE: 0.0}
    for sym in positions:
        s = sleeve_of(state_pos.get(sym, {}))
        out[s] = out.get(s, 0.0) + pos_value(sym)
    return out


def sleeve_room(sleeve, equity, sleeves_cfg, exposure):
    """Dollars still investable in `sleeve` before it hits its target cap."""
    cap = sleeve_target_frac(sleeves_cfg, sleeve) * equity
    return max(0.0, cap - exposure.get(sleeve, 0.0))
