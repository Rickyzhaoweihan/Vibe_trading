#!/usr/bin/env python3
"""Unit tests for the TradingAgents integration glue: deep-confirm veto logic,
core rating->intent mapping, core round-trip outcome attribution, and the
news-context clipper. The actual multi-agent / LLM calls are not exercised
(they're network); we test the pure decision logic around them. No network."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deepresearch
import analyze
import reflect
import signals


class TestUnderlying(unittest.TestCase):
    def test_leveraged_map(self):
        self.assertEqual(deepresearch.underlying_of("TQQQ"), "QQQ")
        self.assertEqual(deepresearch.underlying_of("SQQQ"), "QQQ")
        self.assertEqual(deepresearch.underlying_of("SOXL"), "SOXX")
        self.assertEqual(deepresearch.underlying_of("UPRO"), "SPY")

    def test_passthrough_unknown(self):
        self.assertEqual(deepresearch.underlying_of("NVDA"), "NVDA")


class TestConfirmGate(unittest.TestCase):
    def setUp(self):
        deepresearch._CACHE.clear()
        self._orig = deepresearch.research

    def tearDown(self):
        deepresearch.research = self._orig
        deepresearch._CACHE.clear()

    def _stub(self, rating, ok=True):
        deepresearch.research = lambda t, d, profile="confirm": {
            "ticker": t, "rating": rating, "ok": ok, "stop_loss": None,
            "final_decision": "", "trader_plan": ""}

    def test_bearish_underlying_vetoes(self):
        self._stub("Sell")
        allow, info = deepresearch.confirm_entry("TQQQ", "2026-06-14")
        self.assertFalse(allow)

    def test_underweight_vetoes(self):
        self._stub("Underweight")
        allow, _ = deepresearch.confirm_entry("SOXL", "2026-06-14")
        self.assertFalse(allow)

    def test_bullish_allows(self):
        self._stub("Buy")
        allow, _ = deepresearch.confirm_entry("TQQQ", "2026-06-14")
        self.assertTrue(allow)

    def test_research_failure_fails_open(self):
        self._stub("Hold", ok=False)
        allow, info = deepresearch.confirm_entry("TQQQ", "2026-06-14")
        self.assertTrue(allow)        # fail open — never block the aggressive engine
        self.assertFalse(info["ok"])


class TestRatingToIntent(unittest.TestCase):
    def test_buy_maps_to_core_buy(self):
        it = analyze.rating_to_intent("NVDA", "Buy")
        self.assertEqual(it["side"], "buy")
        self.assertEqual(it["sleeve"], "core")
        self.assertEqual(it["hold_class"], "core")
        self.assertEqual(it["policy_id"], "core_research")
        self.assertEqual(it["target_frac"], analyze.CORE_TARGET["Buy"])

    def test_overweight_smaller_size(self):
        self.assertLess(analyze.rating_to_intent("NVDA", "Overweight")["target_frac"],
                        analyze.rating_to_intent("NVDA", "Buy")["target_frac"])

    def test_sell_maps_to_sell(self):
        self.assertEqual(analyze.rating_to_intent("NVDA", "Sell")["side"], "sell")
        self.assertEqual(analyze.rating_to_intent("NVDA", "Underweight")["side"], "sell")

    def test_hold_is_none(self):
        self.assertIsNone(analyze.rating_to_intent("NVDA", "Hold"))


class TestCoreOutcomes(unittest.TestCase):
    def test_round_trip_attribution(self):
        trades = [
            {"policy_id": "core_research", "symbol": "NVDA", "side": "buy",
             "dollar_amount": 50.0, "ts": "2026-06-01T11:00:00"},
            {"policy_id": "core_research", "symbol": "NVDA", "side": "sell",
             "pnl": 7.5, "ts": "2026-06-09T11:00:00"},
        ]
        out = reflect.core_outcomes(trades)
        self.assertEqual(len(out), 1)
        oc = out[0]
        self.assertEqual(oc["ticker"], "NVDA")
        self.assertAlmostEqual(oc["raw_return"], 0.15)
        self.assertEqual(oc["holding_days"], 8)
        self.assertEqual(oc["dollar_in"], 50.0)

    def test_accumulates_adds_before_sell(self):
        trades = [
            {"policy_id": "core_research", "symbol": "NVDA", "side": "buy",
             "dollar_amount": 30.0, "ts": "2026-06-01T11:00:00"},
            {"policy_id": "core_research", "symbol": "NVDA", "side": "buy",
             "dollar_amount": 20.0, "ts": "2026-06-03T11:00:00"},
            {"policy_id": "core_research", "symbol": "NVDA", "side": "sell",
             "pnl": 5.0, "ts": "2026-06-09T11:00:00"},
        ]
        oc = reflect.core_outcomes(trades)[0]
        self.assertEqual(oc["dollar_in"], 50.0)
        self.assertAlmostEqual(oc["raw_return"], 0.1)

    def test_ignores_non_core_and_open(self):
        trades = [
            {"policy_id": "rsi2_meanrev", "symbol": "TQQQ", "side": "buy",
             "dollar_amount": 50.0, "ts": "2026-06-01T11:00:00"},
            {"policy_id": "core_research", "symbol": "AVGO", "side": "buy",
             "dollar_amount": 40.0, "ts": "2026-06-01T11:00:00"},  # still open
        ]
        self.assertEqual(reflect.core_outcomes(trades), [])


class TestSignalsClip(unittest.TestCase):
    def test_clip_truncates(self):
        self.assertEqual(signals._clip("abcdef", 3), "abc…")
        self.assertEqual(signals._clip("ab", 5), "ab")
        self.assertEqual(signals._clip(None, 5), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
