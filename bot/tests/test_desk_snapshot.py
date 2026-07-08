#!/usr/bin/env python3
"""Unit tests for snapshot.overlay_live — the live-quote overlay onto daily bars.
Guards the preopen 'today's move computed against the wrong day' bug. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import snapshot as SNAP
import regime as rg


class TestOverlayLive(unittest.TestCase):
    def test_preopen_appends_and_keeps_prev_close(self):
        # yfinance has no today bar yet (last bar = yesterday) → append the live
        # print so prev_close stays YESTERDAY, not the day before.
        m = {"NVDA": {"closes": [190.0, 200.0], "dates": ["2026-07-07", "2026-07-08"]}}
        SNAP.overlay_live(m, {"NVDA": {"price": 198.0, "prev_close": 200.0}}, today="2026-07-09")
        ind = rg.indicators(m["NVDA"])
        self.assertEqual(ind["last"], 198.0)
        self.assertEqual(ind["prev_close"], 200.0)          # yesterday preserved

    def test_intraday_overwrites_partial_bar(self):
        # a partial today bar exists → overwrite it, prev_close stays yesterday
        m = {"NVDA": {"closes": [190.0, 200.0, 197.0],
                      "dates": ["2026-07-07", "2026-07-08", "2026-07-09"]}}
        SNAP.overlay_live(m, {"NVDA": {"price": 198.5, "prev_close": 200.0}}, today="2026-07-09")
        ind = rg.indicators(m["NVDA"])
        self.assertEqual(ind["last"], 198.5)
        self.assertEqual(ind["prev_close"], 200.0)

    def test_move_pct_correct_at_preopen(self):
        # the actual bug: a flat premarket must read ~0%, not a two-day move
        m = {"QQQ": {"closes": [700.0, 724.0], "dates": ["2026-07-07", "2026-07-08"]}}
        SNAP.overlay_live(m, {"QQQ": {"price": 725.0, "prev_close": 724.0}}, today="2026-07-09")
        ind = rg.indicators(m["QQQ"])
        move = ind["last"] / ind["prev_close"] - 1.0
        self.assertAlmostEqual(move, 725.0 / 724.0 - 1.0, places=6)   # +0.14%, not +3.6%

    def test_bare_price_quote_tolerated(self):
        m = {"MU": {"closes": [980.0, 1000.0], "dates": ["2026-07-07", "2026-07-08"]}}
        SNAP.overlay_live(m, {"MU": 995.0}, today="2026-07-09")       # legacy shape
        self.assertEqual(rg.indicators(m["MU"])["last"], 995.0)

    def test_missing_symbol_or_price_is_noop(self):
        m = {"NVDA": {"closes": [200.0], "dates": ["2026-07-08"]}}
        SNAP.overlay_live(m, {"NVDA": {"price": None}, "ZZZ": {"price": 5.0}}, today="2026-07-09")
        self.assertEqual(m["NVDA"]["closes"], [200.0])               # unchanged
        self.assertNotIn("ZZZ", m)


if __name__ == "__main__":
    unittest.main()
