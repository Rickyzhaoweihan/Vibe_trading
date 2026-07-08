#!/usr/bin/env python3
"""NYSE trading-calendar gates for the trading bot.

Exit codes are designed for shell gating in run.sh:
  --check-today      exit 0 = trading day, exit 1 = market closed today
  --too-late         exit 0 = past the execution cutoff (do NOT trade), exit 1 = OK
  --sleep-until-open block until 09:32 ET (no-op if already past), always exit 0
"""

import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Official NYSE full-closure holidays, 2026.
HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

# Early closes (13:00 ET), 2026.
HALF_DAYS_2026 = {
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
}

OPEN_EXEC = (9, 32)            # earliest order placement
CUTOFF_FULL = (15, 55)         # latest order placement, full day
CUTOFF_HALF = (12, 55)         # latest order placement, half day

RTH_OPEN = (9, 30)             # regular-hours open
CLOSE_FULL = (16, 0)           # regular-hours close, full day
CLOSE_HALF = (13, 0)           # regular-hours close, half day


def now_et() -> datetime:
    return datetime.now(tz=ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS_2026


def cutoff_for(d: date) -> tuple:
    return CUTOFF_HALF if d in HALF_DAYS_2026 else CUTOFF_FULL


def is_too_late(dt: datetime) -> bool:
    h, m = cutoff_for(dt.date())
    return (dt.hour, dt.minute) >= (h, m)


def within_exec_window(dt: datetime) -> bool:
    if not is_trading_day(dt.date()):
        return False
    return (dt.hour, dt.minute) >= OPEN_EXEC and not is_too_late(dt)


def sleep_until_open() -> None:
    dt = now_et()
    target = dt.replace(hour=OPEN_EXEC[0], minute=OPEN_EXEC[1], second=0, microsecond=0)
    if dt < target:
        time.sleep((target - dt).total_seconds())


def close_for(d: date) -> tuple:
    return CLOSE_HALF if d in HALF_DAYS_2026 else CLOSE_FULL


def is_open_now(dt: datetime = None) -> bool:
    """True during regular trading hours (09:30 .. close) on a trading day.
    Used by the intraday daemon to decide whether to keep ticking."""
    dt = dt or now_et()
    if not is_trading_day(dt.date()):
        return False
    h, m = close_for(dt.date())
    return (dt.hour, dt.minute) >= RTH_OPEN and (dt.hour, dt.minute) < (h, m)


def seconds_to_close(dt: datetime = None) -> int:
    """Seconds until today's regular-hours close. 0 if the market is closed
    or already past the close."""
    dt = dt or now_et()
    if not is_trading_day(dt.date()):
        return 0
    h, m = close_for(dt.date())
    close_dt = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return max(0, int((close_dt - dt).total_seconds()))


def main(argv) -> int:
    if "--check-today" in argv:
        return 0 if is_trading_day(now_et().date()) else 1
    if "--too-late" in argv:
        return 0 if is_too_late(now_et()) else 1
    if "--sleep-until-open" in argv:
        sleep_until_open()
        return 0
    if "--is-open" in argv:
        return 0 if is_open_now() else 1
    if "--seconds-to-close" in argv:
        print(seconds_to_close())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
