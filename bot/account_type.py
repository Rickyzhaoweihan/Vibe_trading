#!/usr/bin/env python3
"""Account-type / day-trade-budget classifier.

The whole aggressive-intraday design hinges on one fact we cannot read from
code: is the auto-trader's account (BOT_ACCOUNT) a CASH or a MARGIN account?

  - MARGIN: SEC Pattern-Day-Trader rule — at most 3 day-trades per rolling
    5 business days while equity < $25k; the 4th triggers a 90-day restriction.
  - CASH:   no PDT rule, but sale proceeds settle T+1. Buying with unsettled
    proceeds and then selling before settlement is a good-faith violation
    (3 in 12 months -> 90-day settled-cash-only restriction).

`classify()` reads the raw `get_accounts` payload the executor captures into
the snapshot and returns a budget object. It FAILS CLOSED: anything it cannot
positively confirm as a margin account with day-trading enabled is treated as
a cash account with ZERO same-day round trips. Nothing here authors orders;
guardrails.py consumes the budget and enforces it.
"""

from __future__ import annotations

import os

# The auto-trader's account id (from .env; placeholder default keeps source clean
# and lets tests match without any real number).
TARGET_ACCOUNT = os.environ.get("BOT_ACCOUNT", "AGENTIC_ACCT")

# Conservative default the rest of the system trusts when detection is
# unavailable or ambiguous. Zero day-trades, settled-cash-only.
CASH_DEFAULT = {
    "type": "cash",
    "day_trade_limit": 0,
    "settlement": "T+1",
    "confirmed": False,
}

PDT_DAYTRADE_LIMIT = 3  # round-trips per rolling 5 business days, equity < $25k
PDT_EQUITY_FLOOR = 25_000.0


def _first(payload):
    """The MCP get_accounts payload may be a list, a {results:[...]} envelope,
    or a single account dict. Return the dict for TARGET_ACCOUNT (or the
    first account) without assuming a shape."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        if "results" in payload and isinstance(payload["results"], list):
            payload = payload["results"]
        else:
            return payload
    if isinstance(payload, list):
        if not payload:
            return None
        for a in payload:
            if isinstance(a, dict) and str(a.get("account_number")) == TARGET_ACCOUNT:
                return a
        return payload[0] if isinstance(payload[0], dict) else None
    return None


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "enabled", "active")
    return False


def classify(accounts_payload, *, equity=None):
    """Return a budget dict: {type, day_trade_limit, settlement, confirmed}.

    Fails closed to CASH_DEFAULT unless the payload positively shows a margin
    account. `equity` (if known) is used to decide whether the PDT limit even
    applies — accounts >= $25k are not pattern-day-trader-restricted, but we
    keep the conservative 3/5-day cap unless equity clearly clears the floor.
    """
    acct = _first(accounts_payload)
    if not isinstance(acct, dict):
        return dict(CASH_DEFAULT)

    # Look for an explicit account-type signal under any of the common keys.
    type_str = ""
    for k in ("type", "account_type", "brokerage_account_type"):
        v = acct.get(k)
        if isinstance(v, str) and v:
            type_str = v.strip().lower()
            break

    # A nested margin/instant balances block is a strong margin signal.
    has_margin_block = any(
        isinstance(acct.get(k), dict) and acct.get(k)
        for k in ("margin_balances", "instant_balances")
    )
    margin_enabled = _truthy(acct.get("margin_enabled")) or has_margin_block
    is_margin = type_str == "margin" or margin_enabled

    if not is_margin:
        # cash, unknown, or anything we can't confirm -> strict
        return dict(CASH_DEFAULT)

    eq = equity
    if eq is None:
        eq = acct.get("equity") or acct.get("portfolio_value")
        try:
            eq = float(eq) if eq is not None else None
        except (TypeError, ValueError):
            eq = None

    if eq is not None and eq >= PDT_EQUITY_FLOOR:
        limit = 999  # not PDT-restricted; still capped elsewhere by trade budget
    else:
        limit = PDT_DAYTRADE_LIMIT

    return {
        "type": "margin",
        "day_trade_limit": limit,
        "settlement": "margin",
        "confirmed": True,
    }


# ---- rolling 5-business-day day-trade ledger helpers --------------------

def _bdays_back(d, n):
    """Return the date n business days before d (inclusive window edge)."""
    from datetime import timedelta
    cur, stepped = d, 0
    while stepped < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            stepped += 1
    return cur


def daytrades_used(pdt_ledger, today):
    """Count day-trades recorded within the rolling 5-business-day window
    ending today. `pdt_ledger` maps ISO date -> count."""
    from datetime import date as _date
    window_start = _bdays_back(today, 4)  # today + 4 prior bdays = 5-day window
    used = 0
    for iso, n in (pdt_ledger or {}).items():
        try:
            d = _date.fromisoformat(iso)
        except ValueError:
            continue
        if window_start <= d <= today:
            used += int(n)
    return used


def daytrade_budget(accounts_payload, pdt_ledger, today, *, equity=None):
    """Convenience: remaining same-day round trips allowed right now."""
    info = classify(accounts_payload, equity=equity)
    if info["type"] != "margin":
        return 0, info
    remaining = max(0, info["day_trade_limit"] - daytrades_used(pdt_ledger, today))
    return remaining, info
