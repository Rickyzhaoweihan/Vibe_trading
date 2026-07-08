#!/usr/bin/env python3
"""Desk health / error warnings.

Two jobs:
  1. In-run checks — detect a DEGRADED run (data fetch came back empty/spotty,
     deep research failed for the names) so the desk warns instead of quietly
     shipping a bad note.
  2. Heartbeat + watchdog — every run stamps a heartbeat; a separate watchdog
     pass warns if an expected run (e.g. the preopen note) never happened today
     (machine asleep, plist not loaded, crash before completion).

Pure functions (check_market, check_research, stale) are unit-tested; the
heartbeat read/write touch one small JSON file.
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

HEARTBEAT_PATH = conf.LOGS_DIR / "desk_heartbeat.json"


# ---- in-run checks (pure) -----------------------------------------------

def check_market(market, expected_syms, *, min_frac=0.5):
    """Warn if too few of the expected symbols resolved to price data."""
    if not expected_syms:
        return []
    resolved = sum(1 for s in expected_syms if (market.get(s, {}) or {}).get("closes"))
    frac = resolved / len(expected_syms)
    if resolved == 0:
        return ["market data fetch returned NOTHING (yfinance down / no network) — "
                "treat this note as unreliable"]
    if frac < min_frac:
        return [f"only {resolved}/{len(expected_syms)} symbols resolved — price data spotty"]
    return []


def check_positions(positions_doc, today, *, max_age_days=4):
    """Warn LOUDLY when the book snapshot can't be trusted — missing, the offline
    seed fallback, undated, or stale — so the desk never silently analyzes a book
    the user no longer holds. Returns a 'POSITIONS:'-prefixed warning (caller
    treats it as critical) or [] when the snapshot is fresh and live."""
    if not positions_doc or not positions_doc.get("positions"):
        return ["POSITIONS: no book snapshot — cannot read the account; treat this note as unreliable"]
    if positions_doc.get("source") == "seed-fallback":
        return ["POSITIONS: live account fetch FAILED — using the offline seed; holdings/sizes may be wrong"]
    as_of = positions_doc.get("as_of")
    if not as_of:
        return ["POSITIONS: snapshot has no as_of date — cannot confirm it reflects the live account"]
    try:
        from datetime import date as _date
        a = _date(*map(int, as_of.split("-")))
        n = _date(*map(int, today.split("-")))
        age = (n - a).days
        if age > max_age_days:
            return [f"POSITIONS: book snapshot is {age}d old (as_of {as_of}) — refresh before trusting sizes/calls"]
    except Exception:
        return [f"POSITIONS: unparseable as_of '{as_of}' — cannot confirm freshness"]

    # Plausibility: the book is written by an LLM relay, so sanity-check the NUMBERS
    # before every dollar figure is derived from them. These flag corruption /
    # hallucination without false-positiving on any normal book.
    cash = positions_doc.get("cash", 0.0)
    if isinstance(cash, (int, float)) and cash < 0:
        return [f"POSITIONS: negative cash ${cash:,.0f} in snapshot — likely a bad relay read; don't trust sizing"]
    poss = positions_doc.get("positions", [])
    if len(poss) > 40:
        return [f"POSITIONS: {len(poss)} positions in snapshot — implausible, likely a bad relay read"]
    for p in poss:
        if not p.get("symbol"):
            return ["POSITIONS: a holding has no symbol — snapshot is malformed"]
        q = p.get("quantity")
        if not isinstance(q, (int, float)) or q <= 0:
            return [f"POSITIONS: {p.get('symbol')} has bad quantity {q!r} — snapshot is unreliable"]
    return []


def check_research(calls, research):
    """Warn if a deep-research run ERRORED for the names it actually attempted.

    A call is only a failure if it was researched this run and errored (ok False,
    not carried). Carried-forward verdicts (`carried`) are a deliberate cost-saving
    reuse of a prior deep call, and names never selected this run aren't failures —
    counting either would fire a false alarm on a normal selective day."""
    if not research or not calls:
        return []
    attempted = [c for c in calls if c.get("ok") is True or
                 (c.get("ok") is False and not c.get("carried") and "research error" in (c.get("reason") or ""))]
    if not attempted:
        return []
    failed = [c for c in attempted if not c.get("ok")]
    if len(failed) == len(attempted):
        return ["deep research FAILED for ALL attempted names (API key / network / TradingAgents?) — "
                "calls fell back to technical-only"]
    if len(failed) >= max(1, len(attempted) // 2):
        return [f"deep research failed for {len(failed)}/{len(attempted)} attempted names — partial coverage"]
    return []


def stale(heartbeat, today):
    """True if a heartbeat is missing, not for `today`, or not ok."""
    if not heartbeat:
        return True
    return heartbeat.get("date") != today or not heartbeat.get("ok", False)


# ---- heartbeat ----------------------------------------------------------

def read_all():
    try:
        return json.loads(HEARTBEAT_PATH.read_text())
    except Exception:
        return {}


def write_heartbeat(mode, *, date, at, ok=True, warnings=None):
    hb = read_all()
    hb[mode] = {"date": date, "at": at, "ok": bool(ok), "warnings": warnings or []}
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(json.dumps(hb, indent=2))
    except Exception:
        pass


def watchdog(*, today, modes=("preopen",), lenient_modes=("monitor", "wrap"),
             max_stale_days=4):
    """Warnings for expected desk modes that went missing.

    `modes` are checked STRICTLY (must have completed ok today) — used for the
    preopen note the morning watchdog can see. `lenient_modes` (monitor, wrap) run
    later than the morning watchdog, so they can't be checked same-day; instead we
    flag them only if their last heartbeat is more than `max_stale_days` old — this
    catches a persistently dark intraday monitor or after-close note without
    false-alarming on the normal timing gap or weekends."""
    from datetime import date as _date
    hb = read_all()
    out = []
    for m in modes:
        if stale(hb.get(m), today):
            last = (hb.get(m) or {}).get("at", "never")
            out.append(f"desk '{m}' did NOT complete today (last ok run: {last})")
    for m in lenient_modes:
        d = (hb.get(m) or {}).get("date")
        try:
            age = (_date(*map(int, today.split("-"))) - _date(*map(int, d.split("-")))).days
        except Exception:
            age = 999
        if age > max_stale_days:
            last = (hb.get(m) or {}).get("at", "never")
            out.append(f"desk '{m}' has not run in {age}d (last: {last}) — "
                       f"intraday/after-close coverage may be dark")
    return out


def _main():
    import argparse
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--modes", nargs="*", default=["preopen"])
    args = ap.parse_args()
    today = datetime.now(et).strftime("%Y-%m-%d")
    if args.watchdog:
        warns = watchdog(today=today, modes=tuple(args.modes))
        if warns:
            body = "\n".join("• " + w for w in warns)
            print(body)
            try:
                import subprocess
                py = str(conf.ROOT / ".venv" / "bin" / "python")
                subprocess.run([py, str(BOT_DIR / "notify.py"), "Desk WATCHDOG"],
                               input=body, text=True, timeout=90)
            except Exception:
                pass
            sys.exit(1)
        print("watchdog OK")
    else:
        print(json.dumps(read_all(), indent=2))


if __name__ == "__main__":
    _main()
