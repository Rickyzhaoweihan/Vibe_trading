#!/usr/bin/env python3
"""Unit tests for desk/news.py — the event digest assembles from the (mocked)
signals layer and never raises. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import news as NEWS
import signals


class TestEventDigest(unittest.TestCase):
    def setUp(self):
        self._m = (signals.macro_digest, signals.ticker_news, signals.sentiment)
        self._e = NEWS.L4.earnings_days_map

    def tearDown(self):
        signals.macro_digest, signals.ticker_news, signals.sentiment = self._m
        NEWS.L4.earnings_days_map = self._e

    def test_assembles_all_sections(self):
        signals.macro_digest = lambda date, **k: "Fed hikes; CPI hot"
        signals.ticker_news = lambda t, date, **k: f"{t} up on AI demand"
        signals.sentiment = lambda t, **k: "Bullish: 10"
        NEWS.L4.earnings_days_map = lambda tickers, **k: {"NVDA": 2}
        out = NEWS.event_digest("2026-07-14", ["NVDA", "MU"], ["ARM"])
        self.assertIn("EVENT WATCH", out)
        self.assertIn("MACRO NEWS", out)
        self.assertIn("Fed hikes", out)
        self.assertIn("PER-NAME NEWS", out)
        self.assertIn("NVDA up on AI demand", out)
        self.assertIn("SENTIMENT", out)
        self.assertIn("UPCOMING EARNINGS", out)
        self.assertIn("NVDA: earnings in 2d", out)

    def test_empty_sources_never_raises(self):
        signals.macro_digest = lambda date, **k: ""
        signals.ticker_news = lambda t, date, **k: ""
        signals.sentiment = lambda t, **k: ""
        NEWS.L4.earnings_days_map = lambda tickers, **k: {}
        out = NEWS.event_digest("2026-07-14", ["NVDA"], [])
        # even with everything empty, the event-watch checklist is always present
        self.assertIn("EVENT WATCH", out)

    def test_source_exception_is_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        signals.macro_digest = boom
        signals.ticker_news = boom
        signals.sentiment = boom
        NEWS.L4.earnings_days_map = boom
        # earnings/macro/per-name all raise → digest still returns (event watch)
        out = NEWS.event_digest("2026-07-14", ["NVDA"], [])
        self.assertIn("EVENT WATCH", out)

    def test_bounded_length(self):
        signals.macro_digest = lambda date, **k: "x" * 5000
        signals.ticker_news = lambda t, date, **k: "y" * 5000
        signals.sentiment = lambda t, **k: "z" * 5000
        NEWS.L4.earnings_days_map = lambda tickers, **k: {}
        out = NEWS.event_digest("2026-07-14", ["NVDA", "MU"], [], max_chars=1500)
        self.assertLessEqual(len(out), 1501)   # _clip appends a 1-char ellipsis


if __name__ == "__main__":
    unittest.main()
