#!/usr/bin/env python3
"""Unit tests for guardrails.py and trading_calendar.py. Pure Python, no network."""

import sys
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import guardrails as g
import trading_calendar as cal

ET = ZoneInfo("America/New_York")
NOW_ET = datetime(2026, 6, 11, 10, 0, tzinfo=ET)  # Thursday, mid-session
NOW_UTC = NOW_ET.astimezone(timezone.utc)


def snap(cash=500.0, positions=None, quotes=None, account="AGENTIC_ACCT", age_min=1):
    return {
        "account_number": account,
        "cash": cash,
        "positions": positions or [],
        "quotes": quotes or {},
        "fetched_at": (NOW_UTC - timedelta(minutes=age_min)).isoformat(),
    }


def dec(*items):
    return {"decisions": [dict(ticker=t, rating=r, **kw) for t, r, kw in items]}


def state(positions=None, cooldowns=None, daily=None):
    return {"positions": positions or {}, "cooldowns": cooldowns or {},
            "daily_trades": daily or {}}


def run(s, d, st, now_et=NOW_ET):
    return g.validate(s, d, st, now_et=now_et, now_utc=NOW_UTC)


class TestCalendar(unittest.TestCase):
    def test_holidays_and_weekends(self):
        self.assertFalse(cal.is_trading_day(date(2026, 7, 3)))    # July 4 observed
        self.assertFalse(cal.is_trading_day(date(2026, 11, 26)))  # Thanksgiving
        self.assertFalse(cal.is_trading_day(date(2026, 6, 13)))   # Saturday
        self.assertTrue(cal.is_trading_day(date(2026, 6, 11)))    # Thursday

    def test_cutoffs(self):
        full = datetime(2026, 6, 11, 15, 56, tzinfo=ET)
        self.assertTrue(cal.is_too_late(full))
        self.assertFalse(cal.is_too_late(datetime(2026, 6, 11, 15, 54, tzinfo=ET)))
        half = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
        self.assertTrue(cal.is_too_late(half))   # half-day cutoff 12:55

    def test_exec_window(self):
        self.assertFalse(cal.within_exec_window(datetime(2026, 6, 11, 9, 31, tzinfo=ET)))
        self.assertTrue(cal.within_exec_window(datetime(2026, 6, 11, 9, 32, tzinfo=ET)))
        self.assertFalse(cal.within_exec_window(datetime(2026, 6, 13, 10, 0, tzinfo=ET)))

    def test_is_open_now(self):
        self.assertTrue(cal.is_open_now(datetime(2026, 6, 11, 10, 0, tzinfo=ET)))
        self.assertFalse(cal.is_open_now(datetime(2026, 6, 11, 9, 0, tzinfo=ET)))   # pre-open
        self.assertFalse(cal.is_open_now(datetime(2026, 6, 11, 16, 0, tzinfo=ET)))  # at close
        self.assertFalse(cal.is_open_now(datetime(2026, 6, 13, 10, 0, tzinfo=ET)))  # Saturday

    def test_seconds_to_close(self):
        self.assertEqual(cal.seconds_to_close(datetime(2026, 6, 11, 15, 0, tzinfo=ET)), 3600)
        self.assertEqual(cal.seconds_to_close(datetime(2026, 6, 11, 16, 30, tzinfo=ET)), 0)
        # half-day closes at 13:00
        self.assertEqual(cal.seconds_to_close(datetime(2026, 11, 27, 12, 0, tzinfo=ET)), 3600)


class TestGuardrails(unittest.TestCase):
    def test_wrong_account_blocks_everything(self):
        orders, rej, note = run(snap(account="OTHER_ACCT"),
                                dec(("PLTR", "Buy", {})), state())
        self.assertEqual(orders, [])
        self.assertEqual(note, "wrong account")

    def test_outside_window_blocks(self):
        late = datetime(2026, 6, 11, 16, 30, tzinfo=ET)
        orders, _, note = run(snap(), dec(("PLTR", "Buy", {})), state(), now_et=late)
        self.assertEqual(orders, [])
        self.assertEqual(note, "outside execution window")

    def test_stale_snapshot_blocks(self):
        orders, _, note = run(snap(age_min=30), dec(("PLTR", "Buy", {})), state())
        self.assertEqual(orders, [])
        self.assertEqual(note, "stale snapshot")

    def test_buy_sized_to_35pct_and_cash_buffer(self):
        orders, _, _ = run(snap(cash=500.0), dec(("PLTR", "Buy", {})), state())
        self.assertEqual(len(orders), 1)
        # 35% of $500 equity = $175, under cash-buffer limit of $490
        self.assertEqual(orders[0]["dollar_amount"], "175.00")
        self.assertEqual(orders[0]["side"], "buy")
        self.assertEqual(orders[0]["type"], "market")

    def test_buy_clipped_by_cash_buffer(self):
        # equity $500 (cash 14 + position 486) -> 35% = $175, but spendable = $4
        pos = [{"symbol": "VST", "quantity": 3.3, "market_value": 486.0}]
        orders, rej, _ = run(snap(cash=14.0, positions=pos),
                             dec(("PLTR", "Buy", {})), state())
        self.assertEqual(orders, [])
        self.assertIn("min", rej[0][1])

    def test_position_room_respected(self):
        # PLTR already 30% of equity -> room only 5%
        pos = [{"symbol": "PLTR", "quantity": 1.0, "market_value": 300.0}]
        orders, _, _ = run(snap(cash=700.0, positions=pos),
                           dec(("PLTR", "Buy", {})), state())
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["dollar_amount"], "50.00")  # 35%*1000 - 300

    def test_min_hold_blocks_early_sell(self):
        pos = [{"symbol": "PLTR", "quantity": 1.0, "market_value": 130.0}]
        st = state(positions={"PLTR": {"entry_date": "2026-06-10", "stop_loss": 100.0}})
        orders, rej, _ = run(snap(positions=pos, quotes={"PLTR": 130.0}),
                             dec(("PLTR", "Sell", {})), st)
        self.assertEqual(orders, [])
        self.assertIn("min-hold", rej[0][1])

    def test_stop_hit_overrides_min_hold(self):
        pos = [{"symbol": "PLTR", "quantity": 1.0, "shares_available_for_sells": 1.0,
                "market_value": 95.0}]
        st = state(positions={"PLTR": {"entry_date": "2026-06-10", "stop_loss": 100.0}})
        orders, _, _ = run(snap(positions=pos, quotes={"PLTR": 95.0}),
                           dec(("PLTR", "Hold", {})), st)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["side"], "sell")

    def test_sell_after_min_hold(self):
        pos = [{"symbol": "PLTR", "quantity": 2.0, "shares_available_for_sells": 2.0,
                "market_value": 260.0}]
        st = state(positions={"PLTR": {"entry_date": "2026-06-05"}})
        orders, _, _ = run(snap(positions=pos, quotes={"PLTR": 130.0}),
                           dec(("PLTR", "Sell", {})), st)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["quantity"], "2.000000")

    def test_underweight_does_not_exit(self):
        pos = [{"symbol": "PLTR", "quantity": 1.0, "market_value": 130.0}]
        st = state(positions={"PLTR": {"entry_date": "2026-06-01"}})
        orders, _, _ = run(snap(positions=pos, quotes={"PLTR": 130.0}),
                           dec(("PLTR", "Underweight", {})), st)
        self.assertEqual(orders, [])

    def test_daily_trade_budget(self):
        st = state(daily={"2026-06-11": 3})  # 3 already today -> budget 1
        orders, rej, _ = run(snap(cash=500.0),
                             dec(("PLTR", "Buy", {}), ("VST", "Buy", {})), st)
        self.assertEqual(len(orders), 1)
        self.assertTrue(any("budget" in r[1] for r in rej))

    def test_cooldown_blocks_reentry(self):
        st = state(cooldowns={"VST": "2026-06-12"})
        orders, rej, _ = run(snap(cash=500.0), dec(("VST", "Buy", {})), st)
        self.assertEqual(orders, [])
        self.assertIn("cooldown", rej[0][1])

    def test_no_leverage_total_buys_bounded_by_cash(self):
        orders, _, _ = run(snap(cash=200.0),
                           dec(("PLTR", "Buy", {}), ("VST", "Buy", {}),
                               ("AVGO", "Buy", {})), state())
        total = sum(float(o["dollar_amount"]) for o in orders)
        self.assertLessEqual(total, 190.0)  # cash - $10 buffer

    def test_overweight_skipped_when_cash_low(self):
        # equity $500 (cash 150 + position 350) -> cash < 40% of equity
        pos = [{"symbol": "VST", "quantity": 2.4, "market_value": 350.0}]
        orders, rej, _ = run(snap(cash=150.0, positions=pos),
                             dec(("PLTR", "Overweight", {})), state())
        self.assertEqual(orders, [])
        self.assertIn("higher-conviction", rej[0][1])

    def test_buy_ranked_above_overweight(self):
        orders, _, _ = run(snap(cash=500.0),
                           dec(("VST", "Overweight", {}), ("PLTR", "Buy", {})), state())
        self.assertEqual(orders[0]["symbol"], "PLTR")

    def test_every_order_is_agentic_account_market_regular(self):
        orders, _, _ = run(snap(cash=500.0), dec(("PLTR", "Buy", {})), state())
        for o in orders:
            self.assertEqual(o["account_number"], "AGENTIC_ACCT")
            self.assertEqual(o["type"], "market")
            self.assertEqual(o["market_hours"], "regular_hours")


if __name__ == "__main__":
    unittest.main(verbosity=2)
