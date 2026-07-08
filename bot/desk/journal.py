#!/usr/bin/env python3
"""L7 — Memory / accountability.

Every actionable call the desk makes is journaled (ticker, action, thesis,
entry/stop/target). An outcome-review pass prices the open calls, scores them,
and reports a running hit-rate so the desk stays honest and improves. This is
advisory bookkeeping only — it tunes nothing that trades.

score_call is pure/tested; review_outcomes() does the price fetch.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import regime as rg

ET = ZoneInfo("America/New_York")

# Actions worth tracking for accountability (directional, actionable).
TRACKED = {"BUY", "NEW_BUY", "TRIM", "SELL"}


def log_calls(calls, *, date, path=None):
    """Append actionable calls to the journal, ONE ROW PER EPISODE (not per run).

    The desk re-emits the same call daily; logging every run made accountability
    double-count (a call repeated 10 days looked like 10 decisions). We only write
    a call when it OPENS a new episode — i.e. the ticker's last journaled action
    differs (or it was never journaled). A held recommendation that persists
    unchanged is logged once; a flip (BUY→TRIM, TRIM→SELL) opens a new episode."""
    path = Path(path) if path else conf.JOURNAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    last_action = {}
    for rec in load_journal(path):
        last_action[rec.get("ticker")] = rec.get("action")
    n = 0
    with path.open("a") as f:
        for c in calls:
            a, sym = c.get("action"), c.get("ticker")
            if a not in TRACKED or last_action.get(sym) == a:
                continue                       # not actionable, or same open episode
            rec = {"date": date, "ticker": sym, "action": a,
                   "rating": c.get("rating"), "entry": c.get("entry"),
                   "stop": c.get("stop") or c.get("stop_loss"),
                   "target": c.get("target"), "reason": c.get("reason")}
            f.write(json.dumps(rec) + "\n")
            last_action[sym] = a
            n += 1
    return n


def score_call(call, current_price):
    """Outcome of one journaled call given the current price.

    Long-side actions (BUY/NEW_BUY/KEEP) score the move up; exit actions
    (SELL/TRIM) score the avoided downside (a fall after a sell counts as a win).
    Returns {status, return} where status is open|target_hit|stop_hit|closed.
    """
    entry = call.get("entry")
    if not entry or not current_price:
        return {"status": "open", "return": None}
    side = -1 if call.get("action") in ("SELL", "TRIM") else 1
    ret = (current_price / entry - 1.0) * side
    status = "open"
    tgt, stop = call.get("target"), call.get("stop")
    if tgt and ((side == 1 and current_price >= tgt) or (side == -1 and current_price <= tgt)):
        status = "target_hit"
    elif stop and ((side == 1 and current_price <= stop) or (side == -1 and current_price >= stop)):
        status = "stop_hit"
    return {"status": status, "return": round(ret, 4)}


def load_journal(path=None):
    path = Path(path) if path else conf.JOURNAL_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def repeat_counts(calls, journal, *, today, lookback_days=5):
    """How many consecutive prior days each call's (ticker, action) already
    appeared in the journal — so the desk can say 'third day I'm telling you this'
    instead of repeating itself as if the idea were new. Pure.

    Returns {ticker: n_prior_days} for the calls whose action matches.
    """
    from datetime import date as _date, timedelta

    def _d(s):
        try:
            return _date(*map(int, s.split("-")))
        except Exception:
            return None

    td = _d(today)
    if td is None:
        return {}
    by_day = {}
    for rec in journal:
        d = _d(rec.get("date", ""))
        if d and (td - d).days <= lookback_days:
            by_day.setdefault(d, set()).add((rec.get("ticker"), rec.get("action")))
    out = {}
    for c in calls:
        key = (c.get("ticker"), c.get("action"))
        n = 0
        for back in range(1, lookback_days + 1):
            day = td - timedelta(days=back)
            if day.weekday() >= 5:            # weekends don't break the streak
                continue
            if key in by_day.get(day, set()):
                n += 1
            else:
                break
        if n:
            out[c.get("ticker")] = n
    return out


def review_outcomes(path=None):
    """Price every journaled call with an entry and summarize hit-rate + avg
    return. Best-effort; returns a summary dict."""
    rows = [c for c in load_journal(path) if c.get("entry")]
    # Collapse to ONE row per (ticker, action) — the latest — so legacy daily
    # duplicates don't dominate the hit-rate (each distinct decision counts once).
    latest = {}
    for c in rows:
        latest[(c.get("ticker"), c.get("action"))] = c
    calls = list(latest.values())
    if not calls:
        return {"n": 0, "scored": 0, "hit_rate": None, "avg_return": None, "calls": []}
    market = rg.fetch_market(list({c["ticker"] for c in calls if c.get("ticker")}))
    scored, rets, wins = [], [], 0
    for c in calls:
        ind = rg.indicators(market.get(c.get("ticker"), {}))
        res = score_call(c, ind.get("last"))
        if res["return"] is not None:
            rets.append(res["return"])
            wins += 1 if res["return"] > 0 else 0
        scored.append({**c, **res})
    return {
        "n": len(calls),
        "scored": len(rets),
        "hit_rate": round(wins / len(rets), 3) if rets else None,
        "avg_return": round(sum(rets) / len(rets), 4) if rets else None,
        "calls": scored,
    }


def main():
    print(json.dumps(review_outcomes(), indent=2, default=str))


if __name__ == "__main__":
    main()
