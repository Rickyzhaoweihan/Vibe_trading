#!/usr/bin/env python3
"""Unit tests for account_type.py — fail-closed cash/margin classification and
the rolling 5-business-day day-trade ledger. No network."""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import account_type as at


class TestClassify(unittest.TestCase):
    def test_none_fails_closed_to_cash(self):
        info = at.classify(None)
        self.assertEqual(info["type"], "cash")
        self.assertEqual(info["day_trade_limit"], 0)
        self.assertFalse(info["confirmed"])

    def test_explicit_cash(self):
        info = at.classify({"account_number": "AGENTIC_ACCT", "type": "cash"})
        self.assertEqual(info["type"], "cash")
        self.assertEqual(info["day_trade_limit"], 0)

    def test_unknown_type_fails_closed(self):
        info = at.classify({"account_number": "AGENTIC_ACCT", "foo": "bar"})
        self.assertEqual(info["type"], "cash")

    def test_explicit_margin(self):
        info = at.classify({"type": "margin"})
        self.assertEqual(info["type"], "margin")
        self.assertEqual(info["day_trade_limit"], at.PDT_DAYTRADE_LIMIT)
        self.assertTrue(info["confirmed"])

    def test_margin_balances_block_implies_margin(self):
        info = at.classify({"margin_balances": {"day_trade_buying_power": "100"}})
        self.assertEqual(info["type"], "margin")

    def test_empty_margin_block_is_not_margin(self):
        # an empty dict must NOT be read as margin
        info = at.classify({"margin_balances": {}})
        self.assertEqual(info["type"], "cash")

    def test_results_envelope_and_account_selection(self):
        payload = {"results": [
            {"account_number": "OTHER_ACCT", "type": "cash"},
            {"account_number": "AGENTIC_ACCT", "type": "margin"},
        ]}
        self.assertEqual(at.classify(payload)["type"], "margin")

    def test_large_equity_margin_not_pdt_capped(self):
        info = at.classify({"type": "margin"}, equity=30_000.0)
        self.assertGreater(info["day_trade_limit"], at.PDT_DAYTRADE_LIMIT)


class TestLedger(unittest.TestCase):
    def test_window_excludes_old_trades(self):
        today = date(2026, 6, 11)  # Thursday
        ledger = {
            "2026-06-11": 1,           # today
            "2026-06-10": 1,           # in window
            "2026-06-01": 5,           # well outside 5-bday window
        }
        self.assertEqual(at.daytrades_used(ledger, today), 2)

    def test_budget_margin(self):
        payload = {"type": "margin"}
        ledger = {"2026-06-11": 1}
        rem, info = at.daytrade_budget(payload, ledger, date(2026, 6, 11))
        self.assertEqual(rem, at.PDT_DAYTRADE_LIMIT - 1)
        self.assertEqual(info["type"], "margin")

    def test_budget_cash_is_zero(self):
        rem, info = at.daytrade_budget(None, {}, date(2026, 6, 11))
        self.assertEqual(rem, 0)
        self.assertEqual(info["type"], "cash")

    def test_budget_never_negative(self):
        payload = {"type": "margin"}
        ledger = {"2026-06-11": 9}
        rem, _ = at.daytrade_budget(payload, ledger, date(2026, 6, 11))
        self.assertEqual(rem, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
