#!/usr/bin/env python3
"""Unit tests for desk/sectors.py — stats + the unwind-risk score. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import sectors as L2


def ramp(start, step, n):
    return [start + step * i for i in range(n)]


class TestStats(unittest.TestCase):
    def test_pearson_perfect(self):
        a = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(L2.pearson(a, [2, 4, 6, 8, 10]), 1.0, places=6)

    def test_pearson_anti(self):
        self.assertAlmostEqual(L2.pearson([1, 2, 3, 4], [4, 3, 2, 1]), -1.0, places=6)

    def test_avg_pairwise_corr(self):
        s = [1, 2, 3, 4, 5]
        c = L2.avg_pairwise_corr([s, [2, 4, 6, 8, 10], [3, 6, 9, 12, 15]])
        self.assertAlmostEqual(c, 1.0, places=6)

    def test_is_extended_uptrend(self):
        self.assertTrue(L2.is_extended(ramp(100, 1.5, 80)))   # steady rip -> RSI ~100

    def test_is_extended_downtrend_false(self):
        self.assertFalse(L2.is_extended(ramp(200, -1.0, 80)))


class TestUnwindScore(unittest.TestCase):
    def test_calm_low(self):
        r = L2.unwind_risk_score(mtum_minus_spy=0.03, frac_extended=0.0,
                                 avg_corr=0.3, vix=13.0, breadth_divergence=False)
        self.assertEqual(r["band"], "low")
        self.assertEqual(r["score"], 0.0)

    def test_crash_high(self):
        r = L2.unwind_risk_score(mtum_minus_spy=-0.05, frac_extended=1.0,
                                 avg_corr=0.85, vix=28.0, breadth_divergence=True)
        self.assertEqual(r["band"], "high")
        self.assertGreaterEqual(r["score"], 65.0)
        self.assertTrue(r["reasons"])

    def test_elevated_middle(self):
        r = L2.unwind_risk_score(mtum_minus_spy=0.0, frac_extended=0.6,
                                 avg_corr=0.85, vix=15.0, breadth_divergence=False)
        self.assertEqual(r["band"], "elevated")


class TestSectorRead(unittest.TestCase):
    def test_ranking_orders_by_excess(self):
        # bench flat-ish; XLK strong, XLU weak
        market = {
            "SPY": {"closes": ramp(100, 0.0, 70)},
            "XLK": {"closes": ramp(100, 1.0, 70)},
            "XLU": {"closes": ramp(100, -0.5, 70)},
        }
        out = L2.sector_read(market, lookback=63)
        self.assertEqual(out["ranked"][0]["sector"], "XLK")
        self.assertIn("XLU", out["laggards"])


if __name__ == "__main__":
    unittest.main()
