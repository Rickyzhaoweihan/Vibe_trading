#!/usr/bin/env python3
"""Unit tests for guardrails.validate_intents — the policy-aware order author
(leveraged sleeve, vol-target sizing, 80/20 sleeves, PDT governor). No network.
Exercised through the public validate() dispatch."""

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import guardrails as g

ET = ZoneInfo("America/New_York")
NOW_ET = datetime(2026, 6, 11, 10, 0, tzinfo=ET)   # Thursday, mid-session
NOW_UTC = NOW_ET.astimezone(timezone.utc)
MARGIN = {"type": "margin"}
CASH = {"type": "cash"}


def snap(cash=400.0, settled=None, positions=None, quotes=None,
         account="AGENTIC_ACCT", age_min=1, accounts_raw=MARGIN):
    return {
        "account_number": account,
        "cash": cash,
        "settled_cash": cash if settled is None else settled,
        "positions": positions or [],
        "quotes": quotes or {},
        "accounts_raw": accounts_raw,
        "fetched_at": (NOW_UTC - timedelta(minutes=age_min)).isoformat(),
    }


def intent(ticker, side, **kw):
    base = dict(ticker=ticker, side=side, policy_id="rsi2_meanrev",
                sleeve="aggressive", hold_class="swing_lev",
                target_frac=0.25, stop_pct=0.06, realized_vol=0.50)
    base.update(kw)
    return base


def decs(*its):
    return {"intents": list(its)}


def state(positions=None, pdt=None, daily=None, sleeves=None):
    return {
        "positions": positions or {},
        "pdt_ledger": pdt or {},
        "daily_trades": daily or {},
        "sleeves": sleeves or {"aggressive": {"target_frac": 0.80},
                               "core": {"target_frac": 0.20}},
    }


def run(s, d, st, now_et=NOW_ET):
    return g.validate(s, d, st, now_et=now_et, now_utc=NOW_UTC)


class TestGlobalGates(unittest.TestCase):
    def test_wrong_account(self):
        o, r, note = run(snap(account="OTHER_ACCT"), decs(intent("TQQQ", "buy")), state())
        self.assertEqual(o, [])
        self.assertEqual(note, "wrong account")

    def test_outside_window(self):
        late = datetime(2026, 6, 11, 16, 30, tzinfo=ET)
        o, _, note = run(snap(), decs(intent("TQQQ", "buy")), state(), now_et=late)
        self.assertEqual(o, [])
        self.assertEqual(note, "outside execution window")

    def test_stale_snapshot(self):
        o, _, note = run(snap(age_min=30), decs(intent("TQQQ", "buy")), state())
        self.assertEqual(o, [])
        self.assertEqual(note, "stale snapshot")


class TestSizing(unittest.TestCase):
    def test_vol_target_neutral(self):
        # realized_vol == VOL_TARGET -> scale 1.0 -> 0.25 * 400 = 100
        o, _, _ = run(snap(cash=400.0), decs(intent("TQQQ", "buy")), state())
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0]["dollar_amount"], "100.00")
        self.assertEqual(o[0]["policy_id"], "rsi2_meanrev")
        self.assertTrue(o[0]["opened_today"])

    def test_vol_target_shrinks_high_vol(self):
        # realized_vol 1.0 -> scale 0.5 -> 0.25 * 0.5 * 400 = 50
        o, _, _ = run(snap(cash=400.0), decs(intent("TQQQ", "buy", realized_vol=1.0)), state())
        self.assertEqual(o[0]["dollar_amount"], "50.00")

    def test_class_max_caps_position(self):
        # huge target_frac clipped to swing_lev max 0.30 * 400 = 120
        o, _, _ = run(snap(cash=400.0),
                      decs(intent("TQQQ", "buy", target_frac=0.99)), state())
        self.assertEqual(o[0]["dollar_amount"], "120.00")

    def test_aggressive_sleeve_cap(self):
        # three different names, each wants 30% -> total capped at 80% of equity
        its = [intent(t, "buy", target_frac=0.30) for t in ("TQQQ", "SOXL", "TECL")]
        o, _, _ = run(snap(cash=400.0), decs(*its), state())
        total = sum(float(x["dollar_amount"]) for x in o)
        self.assertLessEqual(total, 0.80 * 400 + 0.01)

    def test_min_order_rejected(self):
        o, r, _ = run(snap(cash=400.0),
                      decs(intent("TQQQ", "buy", target_frac=0.001)), state())
        self.assertEqual(o, [])
        self.assertTrue(any("min" in x[1] for x in r))


class TestCashAccount(unittest.TestCase):
    def test_buy_capped_by_settled_cash(self):
        # cash account: only $50 settled though buying_power shows 400
        o, _, _ = run(snap(cash=400.0, settled=50.0, accounts_raw=CASH),
                      decs(intent("TQQQ", "buy")), state())
        self.assertEqual(o[0]["dollar_amount"], "40.00")  # 50 - 10 buffer

    def test_daytrade_exit_blocked_on_cash(self):
        # selling a position opened today on a cash account == 0 day-trade budget
        pos = [{"symbol": "TQQQ", "quantity": 1.0,
                "shares_available_for_sells": 1.0, "market_value": 100.0}]
        st = state(positions={"TQQQ": {"entry_date": "2026-06-11", "opened_today": True,
                                       "hold_class": "swing_lev"}})
        o, r, _ = run(snap(cash=300.0, positions=pos, quotes={"TQQQ": 100.0},
                           accounts_raw=CASH),
                      decs(intent("TQQQ", "sell")), st)
        self.assertEqual(o, [])
        self.assertTrue(any("day-trade" in x[1] for x in r))


class TestPdtGovernor(unittest.TestCase):
    def _held_today(self):
        pos = [{"symbol": "TQQQ", "quantity": 1.0,
                "shares_available_for_sells": 1.0, "market_value": 100.0}]
        st = state(positions={"TQQQ": {"entry_date": "2026-06-11", "opened_today": True,
                                       "hold_class": "swing_lev", "stop_loss": 80.0}})
        return pos, st

    def test_margin_allows_daytrade_within_budget(self):
        pos, st = self._held_today()
        o, _, _ = run(snap(cash=300.0, positions=pos, quotes={"TQQQ": 100.0}),
                      decs(intent("TQQQ", "sell")), st)
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0]["side"], "sell")

    def test_margin_budget_exhausted(self):
        pos, st = self._held_today()
        st["pdt_ledger"] = {"2026-06-11": 3}   # 3 used in window -> 0 left
        o, r, _ = run(snap(cash=300.0, positions=pos, quotes={"TQQQ": 100.0}),
                      decs(intent("TQQQ", "sell")), st)
        self.assertEqual(o, [])
        self.assertTrue(any("day-trade" in x[1] for x in r))

    def test_stop_hit_overrides_pdt(self):
        # price below recorded stop -> exit allowed even with zero budget
        pos, st = self._held_today()
        st["pdt_ledger"] = {"2026-06-11": 3}
        o, _, _ = run(snap(cash=300.0, positions=pos, quotes={"TQQQ": 70.0},
                           accounts_raw=CASH),
                      decs(intent("TQQQ", "sell")), st)
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0]["side"], "sell")


class TestHoldClass(unittest.TestCase):
    def test_core_min_hold_blocks_early_exit(self):
        pos = [{"symbol": "FNGU", "quantity": 1.0,
                "shares_available_for_sells": 1.0, "market_value": 50.0}]
        st = state(positions={"FNGU": {"entry_date": "2026-06-10", "hold_class": "core"}})
        o, r, _ = run(snap(cash=300.0, positions=pos, quotes={"FNGU": 50.0}),
                      decs(intent("FNGU", "sell", hold_class="core")), st)
        self.assertEqual(o, [])
        self.assertTrue(any("min-hold" in x[1] for x in r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
