#!/usr/bin/env python3
"""Refresh the desk's view of the LIVE book (account from conf.ACCOUNT / .env).

Advisory only: this READS the account through a headless, read-only `claude -p`
relay (positions/portfolio MCP reads + Write to positions.json — NO order tools).
Before each refresh the current snapshot is rotated to positions_prev.json so the
desk can diff the two and tell the user 'your trade has been monitored'.

  python bot/desk/snapshot.py            # rotate + refresh from the live account
  python bot/desk/snapshot.py --diff     # just print trades since the last snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import portfolio as L5

# Read-only allowlist — the relay can read the book and write the snapshot file,
# and nothing else. No review_/place_/cancel_ order tools anywhere.
SNAP_TOOLS = ("mcp__robinhood-trading__get_equity_positions,"
              "mcp__robinhood-trading__get_portfolio,Read,Write")


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def rotate_prev():
    """Copy the current snapshot to positions_prev.json (the diff baseline)."""
    try:
        if conf.POSITIONS_PATH.exists():
            shutil.copy2(conf.POSITIONS_PATH, conf.POSITIONS_PREV_PATH)
            return True
    except Exception:
        pass
    return False


def refresh(*, timeout=180):
    """Rotate prev, then run the read-only relay to rewrite positions.json from the
    live account. Returns (ok, message). Never raises."""
    rotate_prev()
    try:
        prompt = (BOT_DIR / "prompts" / "desk_snapshot.md").read_text().replace(
            "{{ACCOUNT}}", conf.ACCOUNT)
    except Exception as e:
        return False, f"prompt missing: {e}"
    if not conf.ACCOUNT:
        return False, "DESK_ACCOUNT not set in .env"
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", SNAP_TOOLS, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout, cwd=str(conf.ROOT), env=relay_env())
        out = (r.stdout or "").strip()
        ok = "SNAPSHOT OK" in out and r.returncode == 0
        return ok, (out.splitlines()[-1] if out else f"rc={r.returncode}")
    except Exception as e:
        return False, f"relay failed: {e}"


QUOTE_TOOLS = "mcp__robinhood-trading__get_equity_quotes,Write"


def relay_env():
    """Environment for a `claude -p` relay: strip ANTHROPIC_API_KEY so the CLI
    uses its OWN login (Claude subscription/OAuth), not a raw key from .env. The
    .env key is for the research path only and may be unset/invalid; letting it
    leak onto `claude -p` breaks every relay with 'invalid x-api-key'."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def realtime_quotes(symbols, *, timeout=90):
    """Broker real-time quotes for `symbols` via a read-only `claude -p` relay on
    the SAME Robinhood MCP feed that snapshots the account. Returns
    {symbol: {"price": live, "prev_close": prior-session close}}; {} on any failure
    (caller falls back to yfinance). No order tools — quotes only.

    The prev_close lets `overlay_live` set today's move correctly even at the
    preopen, when yfinance has no today bar yet."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    out_path = conf.DESK_DIR / "_live_quotes.json"
    try:
        out_path.unlink()
    except Exception:
        pass
    prompt = (
        "Use mcp__robinhood-trading__get_equity_quotes to get real-time quotes for these "
        f"symbols: {', '.join(symbols)}. For each, take the more recent of last_trade_price / "
        "last_non_reg_trade_price as the current price, and previous_close as the prior-session "
        "close. Then use Write to save ONLY a JSON object mapping each symbol to a 2-number array "
        f'[price, previous_close], e.g. {{"SQQQ": [38.1, 38.4], "QQQ": [707.5, 705.0]}}, to the '
        f"file {out_path}. Reply 'done'. Call no other tools.")
    try:
        subprocess.run(["claude", "-p", prompt, "--allowedTools", QUOTE_TOOLS,
                        "--output-format", "text"],
                       capture_output=True, text=True, timeout=timeout,
                       cwd=str(conf.ROOT), env=relay_env())
        data = json.loads(out_path.read_text())
        out = {}
        for k, v in data.items():
            if isinstance(v, (list, tuple)) and v and v[0]:
                out[k] = {"price": float(v[0]),
                          "prev_close": float(v[1]) if len(v) > 1 and v[1] else None}
            elif isinstance(v, (int, float)) and v:      # tolerate a bare price
                out[k] = {"price": float(v), "prev_close": None}
        return out
    except Exception:
        return {}
    finally:
        try:
            out_path.unlink()
        except Exception:
            pass


def overlay_live(market, quotes, *, today=None):
    """Overlay broker live quotes onto yfinance daily bars in `market` so
    `indicators()` reads the LIVE price as `last` and the prior session's official
    close as `prev_close`, while keeping the daily history for SMAs. Mutates and
    returns `market`.

    Correctly handles BOTH the preopen (yfinance has no today bar yet → the live
    print is APPENDED so prev_close stays yesterday) and intraday (a partial today
    bar exists → it is OVERWRITTEN), decided by the bar's own date vs `today`.
    `quotes` is {sym: {"price", "prev_close"}} or a bare {sym: price}."""
    for s, q in (quotes or {}).items():
        px = q.get("price") if isinstance(q, dict) else q
        pc = q.get("prev_close") if isinstance(q, dict) else None
        if not px:
            continue
        bars = dict(market.get(s, {}) or {})
        closes = list(bars.get("closes") or [])
        if not closes:
            continue
        dates = list(bars.get("dates") or [])
        last_is_today = bool(today and dates and dates[-1] == today)
        if last_is_today:
            closes[-1] = px                       # replace today's partial/stale bar
        else:
            closes.append(px)                     # no today bar yet (preopen) — add one
            if dates:
                dates.append(today or dates[-1])
                bars["dates"] = dates
        if pc:                                    # pin the official prior-session close
            if len(closes) >= 2:
                closes[-2] = pc
        bars["closes"] = closes
        market[s] = bars
    return market


def diff_since_prev():
    """Trades the user made between positions_prev.json and the current snapshot."""
    prev, curr = _load(conf.POSITIONS_PREV_PATH), _load(conf.POSITIONS_PATH)
    if not prev or not curr:
        return []
    return L5.diff_positions(prev, curr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true", help="print trades since last snapshot, don't refresh")
    args = ap.parse_args()
    if args.diff:
        print(json.dumps(diff_since_prev(), indent=2))
        return
    ok, msg = refresh()
    print(("OK: " if ok else "FAILED: ") + msg)
    if diff_since_prev():
        print("activity: " + json.dumps(diff_since_prev()))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
