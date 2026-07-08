#!/usr/bin/env python3
"""Unit tests for regime.py — pure indicator math + regime classification.
No network."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import regime as r


def ramp(start, step, n):
    return [start + step * i for i in range(n)]


def market_from(index_closes):
    return {"QQQ": {"closes": index_closes,
                    "highs": [c * 1.01 for c in index_closes],
                    "lows": [c * 0.99 for c in index_closes]}}


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(r.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(r.sma([1, 2], 5))

    def test_rsi_all_gains_is_100(self):
        self.assertEqual(r.rsi(ramp(100, 1, 30), 14), 100.0)

    def test_rsi_all_losses_is_low(self):
        self.assertLess(r.rsi(ramp(100, -1, 30), 14), 1.0)

    def test_realized_vol_constant_slope_is_low(self):
        vol = r.realized_vol(ramp(100, 0.5, 60), 20)
        self.assertIsNotNone(vol)
        self.assertLess(vol, 0.10)

    def test_total_return(self):
        self.assertAlmostEqual(r.total_return([100, 110], 1), 0.10)
        self.assertIsNone(r.total_return([100], 5))

    def test_indicators_bundle_keys(self):
        ind = r.indicators({"closes": ramp(100, 0.5, 260)})
        for k in ("last", "sma50", "sma200", "rsi2", "rsi14", "ret_63", "realized_vol"):
            self.assertIn(k, ind)
        self.assertIsNotNone(ind["sma200"])


class TestRegime(unittest.TestCase):
    def test_risk_on_uptrend(self):
        out = r.compute_regime(market_from(ramp(100, 0.5, 260)), vix=14.0)
        self.assertEqual(out["label"], r.RISK_ON_TREND)
        self.assertTrue(out["features"]["above_200sma"])
        self.assertTrue(out["features"]["golden_cross"])

    def test_risk_off_downtrend(self):
        out = r.compute_regime(market_from(ramp(230, -0.5, 260)), vix=20.0)
        self.assertEqual(out["label"], r.RISK_OFF_TREND)
        self.assertFalse(out["features"]["above_200sma"])

    def test_high_vol_chop(self):
        # oscillate hard around 100 -> high realized vol, no clean trend.
        # End on the high tick so last >= sma200 (the risk-off branch needs
        # last < sma200); elevated vix forces the high-vol classification.
        closes = [100 + (12 if i % 2 == 1 else -12) for i in range(260)]
        out = r.compute_regime(market_from(closes), vix=30.0)
        self.assertEqual(out["label"], r.HIGH_VOL_CHOP)
        self.assertTrue(out["features"]["high_vol"])

    def test_insufficient_data_is_neutral(self):
        out = r.compute_regime(market_from(ramp(100, 1, 10)))
        self.assertEqual(out["label"], r.NEUTRAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
