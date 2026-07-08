#!/usr/bin/env python3
"""Unit tests for desk/scout.py — candidate scoring + ranking. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import scout as L3


def ramp(start, step, n):
    return [start + step * i for i in range(n)]


class TestScout(unittest.TestCase):
    def test_score_none_when_short(self):
        self.assertIsNone(L3.score_candidate([1, 2, 3], [1, 2, 3], lookback=63))

    def test_momentum_uptrend(self):
        sc = L3.score_candidate(ramp(100, 1.0, 260), ramp(100, 0.1, 260), lookback=63)
        self.assertTrue(sc["uptrend"])
        self.assertEqual(sc["setup"], "momentum")
        self.assertGreater(sc["excess"], 0)

    def test_reversion_flag(self):
        # long uptrend then a sharp 2-day dip -> RSI2 oversold but still > 200DMA
        closes = ramp(100, 1.0, 258)
        closes += [closes[-1] * 0.90, closes[-1] * 0.88]
        sc = L3.score_candidate(closes, ramp(100, 0.1, 260), lookback=63)
        self.assertTrue(sc["uptrend"])
        self.assertEqual(sc["setup"], "reversion")

    def test_extended_name_penalized_vs_clean_entry(self):
        # a blow-off (far above 50DMA) should NOT outrank a name in a steady uptrend
        # near support, even with higher raw return — this is the −12% loss fix
        blowoff = ramp(100, 1.0, 250) + [400, 460, 530]        # parabolic last leg
        steady = ramp(100, 1.2, 253)                            # strong, linear, near its MA
        bench = ramp(100, 0.1, 253)
        sc_blow = L3.score_candidate(blowoff, bench)
        sc_steady = L3.score_candidate(steady, bench)
        self.assertEqual(sc_blow["entry"], "extended")
        self.assertGreater(sc_steady["score"], sc_blow["score"])

    def test_entry_label_dip_on_oversold(self):
        base = ramp(100, 1.0, 258)
        closes = base + [base[-1] * 0.90, base[-1] * 0.88]      # sharp 2-day dip in an uptrend
        self.assertEqual(L3.score_candidate(closes, ramp(100, 0.1, 260))["entry"], "dip")

    def test_rank_excludes_held_and_orders(self):
        market = {
            "SPY": {"closes": ramp(100, 0.1, 120)},
            "AAA": {"closes": ramp(100, 1.0, 120)},   # strong
            "BBB": {"closes": ramp(100, 0.2, 120)},   # mild
            "CCC": {"closes": ramp(100, -0.5, 120)},  # weak
        }
        ranked = L3.rank_pool(market, ["AAA", "BBB", "CCC"], "SPY",
                              lookback=63, exclude={"AAA"})
        tickers = [r["ticker"] for r in ranked]
        self.assertNotIn("AAA", tickers)
        self.assertEqual(tickers[0], "BBB")          # best of the remaining


if __name__ == "__main__":
    unittest.main()
