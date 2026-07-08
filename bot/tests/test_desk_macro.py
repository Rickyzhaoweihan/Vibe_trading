#!/usr/bin/env python3
"""Unit tests for desk/macro.py — rate direction + exposure mapping. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import macro
import regime as rg


class TestRateDirection(unittest.TestCase):
    def test_rising(self):
        closes = [4.0] * 11 + [4.3]          # last vs closes[-11]=4.0 -> +7.5%
        self.assertEqual(macro.rate_direction(closes), "rising")

    def test_falling(self):
        closes = [4.0] * 11 + [3.7]
        self.assertEqual(macro.rate_direction(closes), "falling")

    def test_flat(self):
        closes = [4.0] * 11 + [4.02]
        self.assertEqual(macro.rate_direction(closes), "flat")

    def test_too_short(self):
        self.assertEqual(macro.rate_direction([4.0, 4.1]), "flat")


class TestExposure(unittest.TestCase):
    def test_risk_on_full(self):
        e = macro.recommend_exposure(rg.RISK_ON_TREND)
        self.assertEqual(e["net_target"], 0.90)
        self.assertNotIn("cash", e["hedges"])

    def test_risk_off_hedged(self):
        e = macro.recommend_exposure(rg.RISK_OFF_TREND)
        self.assertEqual(e["net_target"], 0.30)
        for h in ("duration", "gold", "inverse", "cash"):
            self.assertIn(h, e["hedges"])
        # expressions resolve to concrete tickers
        self.assertIn("TLT", e["expressions"]["duration"])

    def test_rising_rates_cut_exposure(self):
        base = macro.recommend_exposure(rg.NEUTRAL)["net_target"]
        cut = macro.recommend_exposure(rg.NEUTRAL, rates_rising=True)
        self.assertAlmostEqual(cut["net_target"], round(base - 0.15, 2))
        self.assertIn("cash", cut["hedges"])

    def test_high_vol_adds_cash(self):
        e = macro.recommend_exposure(rg.NEUTRAL, high_vol=True)
        self.assertIn("cash", e["hedges"])


if __name__ == "__main__":
    unittest.main()
