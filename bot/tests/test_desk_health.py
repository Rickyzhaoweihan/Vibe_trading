#!/usr/bin/env python3
"""Unit tests for desk/health.py — data/research checks, heartbeat, watchdog."""

import sys
import tempfile
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import health as H


class TestChecks(unittest.TestCase):
    def test_market_empty_is_critical(self):
        w = H.check_market({"A": {}, "B": {}}, ["A", "B"])
        self.assertTrue(w and "NOTHING" in w[0])

    def test_market_spotty(self):
        market = {"A": {"closes": [1, 2]}, "B": {}, "C": {}, "D": {}}
        self.assertTrue(H.check_market(market, ["A", "B", "C", "D"]))

    def test_market_ok(self):
        market = {s: {"closes": [1, 2]} for s in "ABCD"}
        self.assertEqual(H.check_market(market, list("ABCD")), [])

    def test_research_all_failed(self):
        # a real research error (attempted this run) is what counts as failure
        calls = [{"ok": False, "reason": "research error: boom"},
                 {"ok": False, "reason": "research error: boom"}]
        self.assertTrue("FAILED for ALL" in H.check_research(calls, True)[0])

    def test_research_skipped_is_quiet(self):
        self.assertEqual(H.check_research([{"ok": False}], False), [])

    def test_research_all_ok(self):
        self.assertEqual(H.check_research([{"ok": True}, {"ok": True}], True), [])

    def test_carried_forward_not_a_failure(self):
        # quiet names carrying a prior verdict (ok False + carried) are NOT failures
        calls = [{"ok": True, "reason": "deep"},
                 {"ok": False, "carried": True, "reason": "沿用3天前深度研究"},
                 {"ok": False, "reason": "technical hold (deep research skipped)"}]
        self.assertEqual(H.check_research(calls, True), [])

    def test_partial_failure_counts_only_attempted(self):
        # 1 ok, 1 real error, 2 carried => 1/2 attempted failed => partial warning
        calls = [{"ok": True}, {"ok": False, "reason": "research error: x"},
                 {"ok": False, "carried": True}, {"ok": False, "carried": True}]
        w = H.check_research(calls, True)
        self.assertTrue(w and "1/2 attempted" in w[0])


class TestCheckPositions(unittest.TestCase):
    def _doc(self, **kw):
        d = {"source": "live", "as_of": "2026-06-26",
             "positions": [{"symbol": "NVDA", "quantity": 1.0}]}
        d.update(kw)
        return d

    def test_fresh_live_is_quiet(self):
        self.assertEqual(H.check_positions(self._doc(), "2026-06-26"), [])

    def test_missing_book_is_critical(self):
        w = H.check_positions({"positions": []}, "2026-06-26")
        self.assertTrue(w and w[0].startswith("POSITIONS"))

    def test_seed_fallback_is_critical(self):
        w = H.check_positions(self._doc(source="seed-fallback"), "2026-06-26")
        self.assertTrue(w and "fetch FAILED" in w[0])

    def test_no_as_of_is_critical(self):
        w = H.check_positions(self._doc(as_of=None), "2026-06-26")
        self.assertTrue(w and w[0].startswith("POSITIONS"))

    def test_stale_book_flagged(self):
        w = H.check_positions(self._doc(as_of="2026-06-01"), "2026-06-26")
        self.assertTrue(w and "old" in w[0])

    def test_within_age_window_quiet(self):
        self.assertEqual(H.check_positions(self._doc(as_of="2026-06-24"), "2026-06-26"), [])

    def test_negative_cash_flagged(self):
        w = H.check_positions(self._doc(cash=-500), "2026-06-26")
        self.assertTrue(w and "negative cash" in w[0])

    def test_bad_quantity_flagged(self):
        doc = self._doc()
        doc["positions"] = [{"symbol": "NVDA", "quantity": 0}]      # zero/nonsense qty
        w = H.check_positions(doc, "2026-06-26")
        self.assertTrue(w and "bad quantity" in w[0])

    def test_missing_symbol_flagged(self):
        doc = self._doc()
        doc["positions"] = [{"quantity": 1.0}]
        self.assertTrue(H.check_positions(doc, "2026-06-26"))

    def test_plausible_book_quiet(self):
        self.assertEqual(H.check_positions(self._doc(cash=1500.0), "2026-06-26"), [])


class TestStale(unittest.TestCase):
    def test_missing(self):
        self.assertTrue(H.stale(None, "2026-06-24"))

    def test_wrong_date(self):
        self.assertTrue(H.stale({"date": "2026-06-23", "ok": True}, "2026-06-24"))

    def test_not_ok(self):
        self.assertTrue(H.stale({"date": "2026-06-24", "ok": False}, "2026-06-24"))

    def test_fresh_ok(self):
        self.assertFalse(H.stale({"date": "2026-06-24", "ok": True}, "2026-06-24"))


class TestHeartbeatWatchdog(unittest.TestCase):
    def setUp(self):
        self._orig = H.HEARTBEAT_PATH
        self._tmp = tempfile.TemporaryDirectory()
        H.HEARTBEAT_PATH = Path(self._tmp.name) / "hb.json"

    def tearDown(self):
        H.HEARTBEAT_PATH = self._orig
        self._tmp.cleanup()

    def test_roundtrip_and_watchdog_clean(self):
        H.write_heartbeat("preopen", date="2026-06-24", at="2026-06-24T08:01:00", ok=True)
        # no lenient modes checked here => clean when preopen ran today
        self.assertEqual(H.watchdog(today="2026-06-24", modes=("preopen",), lenient_modes=()), [])

    def test_watchdog_flags_missing(self):
        w = H.watchdog(today="2026-06-24", modes=("preopen",), lenient_modes=())   # nothing written
        self.assertTrue(w and "did NOT complete" in w[0])

    def test_watchdog_flags_stale_date(self):
        H.write_heartbeat("preopen", date="2026-06-23", at="x", ok=True)
        self.assertTrue(H.watchdog(today="2026-06-24", modes=("preopen",), lenient_modes=()))

    def test_watchdog_lenient_flags_dark_monitor(self):
        H.write_heartbeat("preopen", date="2026-06-24", at="x", ok=True)
        H.write_heartbeat("monitor", date="2026-06-10", at="x", ok=True)   # 14d stale
        w = H.watchdog(today="2026-06-24", modes=("preopen",),
                       lenient_modes=("monitor",), max_stale_days=4)
        self.assertTrue(any("monitor" in x and "not run" in x for x in w))

    def test_watchdog_lenient_quiet_when_recent(self):
        H.write_heartbeat("preopen", date="2026-06-24", at="x", ok=True)
        H.write_heartbeat("monitor", date="2026-06-23", at="x", ok=True)   # 1d — within window
        self.assertEqual(H.watchdog(today="2026-06-24", modes=("preopen",),
                                    lenient_modes=("monitor",), max_stale_days=4), [])


if __name__ == "__main__":
    unittest.main()
