#!/usr/bin/env python3
"""Unit tests for desk/research.py — action mapping + research selection. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import research as L4


class TestRatingToAction(unittest.TestCase):
    def test_held(self):
        self.assertEqual(L4.rating_to_action("Buy", held=True), "BUY")
        self.assertEqual(L4.rating_to_action("Hold", held=True), "KEEP")
        self.assertEqual(L4.rating_to_action("Underweight", held=True), "TRIM")
        self.assertEqual(L4.rating_to_action("Sell", held=True), "SELL")

    def test_candidate(self):
        self.assertEqual(L4.rating_to_action("Buy", held=False), "NEW_BUY")
        self.assertEqual(L4.rating_to_action("Hold", held=False), "PASS")


class TestConvictionHorizon(unittest.TestCase):
    def test_conviction_by_rating(self):
        self.assertEqual(L4.conviction_of("Buy"), "high")
        self.assertEqual(L4.conviction_of("Overweight"), "medium")
        self.assertEqual(L4.conviction_of("Hold"), "low")

    def test_technical_fallback_is_low(self):
        # a strong nominal rating but no real research → low conviction
        self.assertEqual(L4.conviction_of("Buy", ok=False), "low")

    def test_horizon_by_action(self):
        self.assertIn("now", L4.horizon_of("SELL"))
        self.assertIn("multi-week", L4.horizon_of("BUY"))


class TestPriority(unittest.TestCase):
    def test_imminent_earnings_dominates(self):
        s, reasons = L4.research_priority(held=True, earnings_days=1, weight=0.02)
        self.assertGreaterEqual(s, 50)
        self.assertTrue(any("earnings" in r for r in reasons))

    def test_big_move(self):
        s, _ = L4.research_priority(held=True, move_pct=0.08)
        self.assertGreaterEqual(s, 30)

    def test_extended_into_unwind(self):
        hot, _ = L4.research_priority(held=True, extended=True, unwind_band="high")
        calm, _ = L4.research_priority(held=True, extended=True, unwind_band="low")
        self.assertGreater(hot, calm)

    def test_size_weight_capped(self):
        s, _ = L4.research_priority(held=True, weight=0.50)   # huge position
        self.assertLessEqual(s, 20.0)                          # size component capped

    def test_candidate_base(self):
        s, reasons = L4.research_priority(held=False, scout_score=0.5)
        self.assertEqual(s, 18.0)
        self.assertIn("new idea", reasons)

    def test_staleness_boost(self):
        never, _ = L4.research_priority(held=True, stale=None)     # never researched
        fresh, _ = L4.research_priority(held=True, stale=0)        # researched today
        old, _ = L4.research_priority(held=True, stale=10)         # stale
        self.assertGreater(never, fresh)
        self.assertGreater(old, fresh)

    def test_stale_skipped_by_default(self):
        s, _ = L4.research_priority(held=True)                     # stale defaults to "skip"
        self.assertEqual(s, 0.0)


class TestSelect(unittest.TestCase):
    def test_caps_at_six_and_orders(self):
        items = [{"ticker": "EARN", "held": True, "earnings_days": 1},
                 {"ticker": "MOVE", "held": True, "move_pct": 0.09},
                 {"ticker": "BIG", "held": True, "weight": 0.20},
                 {"ticker": "IDEA", "held": False, "scout_score": 0.4},
                 {"ticker": "Q1", "held": True, "weight": 0.01},
                 {"ticker": "Q2", "held": True, "weight": 0.01},
                 {"ticker": "Q3", "held": True, "weight": 0.01}]
        sel = L4.select_for_research(items, max_n=6)
        self.assertEqual(len(sel), 6)
        self.assertEqual(sel[0]["ticker"], "EARN")     # earnings tomorrow wins
        picked = {s["ticker"] for s in sel}
        self.assertIn("MOVE", picked)
        self.assertIn("BIG", picked)
        # only one of the three quiet 1% holdings is dropped
        self.assertEqual(len(picked & {"Q1", "Q2", "Q3"}), 2)

    def test_flagged_holding_beats_idea(self):
        items = [{"ticker": "IDEA", "held": False, "scout_score": 0.9},
                 {"ticker": "EARN", "held": True, "earnings_days": 1}]
        sel = L4.select_for_research(items, max_n=1)
        self.assertEqual(sel[0]["ticker"], "EARN")

    def test_min_score_filters_quiet_names(self):
        # quiet names (no signal, stale ignored) score 0 -> dropped by the threshold
        items = [{"ticker": "QUIET", "held": True, "weight": 0.02},
                 {"ticker": "MOVER", "held": True, "move_pct": 0.06}]
        sel = L4.select_for_research(items, max_n=8, min_score=25)
        picked = {s["ticker"] for s in sel}
        self.assertIn("MOVER", picked)
        self.assertNotIn("QUIET", picked)

    def test_quiet_day_selects_nobody(self):
        items = [{"ticker": "A", "held": True, "weight": 0.03},
                 {"ticker": "B", "held": True, "weight": 0.02}]
        self.assertEqual(L4.select_for_research(items, max_n=8, min_score=25), [])

    def test_actionable_carried_verdict_stays_fresh(self):
        # a name we've been telling the user to TRIM must be re-researched (feasible
        # instruction vs live price); a carried KEEP need not be
        act, _ = L4.research_priority(held=True, stale=2, actionable_prior=True)
        keep, _ = L4.research_priority(held=True, stale=2, actionable_prior=False)
        self.assertGreater(act, keep)
        sel = L4.select_for_research(
            [{"ticker": "SPCX", "held": True, "stale": 2, "actionable_prior": True},
             {"ticker": "AMZN", "held": True, "stale": 2, "actionable_prior": False}],
            max_n=8, min_score=25)
        self.assertEqual([s["ticker"] for s in sel], ["SPCX"])   # only the trade call refreshes

    def test_actionable_prior_ignored_when_fresh(self):
        # already researched today (stale 0) => don't re-spend even if actionable
        s, _ = L4.research_priority(held=True, stale=0, actionable_prior=True)
        self.assertEqual(s, 0.0)

    def test_traded_name_earns_research(self):
        # a name the user just traded clears the threshold on its own
        s, reasons = L4.research_priority(held=True, traded=True)
        self.assertGreaterEqual(s, 30)
        self.assertTrue(any("交易" in r for r in reasons))
        sel = L4.select_for_research([{"ticker": "X", "held": True, "traded": True}],
                                     max_n=8, min_score=25)
        self.assertEqual([s["ticker"] for s in sel], ["X"])


class TestCoverageLedger(unittest.TestCase):
    def test_verdict_roundtrip_and_stale(self):
        import tempfile
        from pathlib import Path
        import conf
        with tempfile.TemporaryDirectory() as td:
            orig = conf.COVERAGE_PATH
            conf.COVERAGE_PATH = Path(td) / "coverage.json"
            try:
                L4.mark_researched([{"ticker": "MU", "ok": True, "rating": "Underweight",
                                     "action": "TRIM", "stop_loss": 900.0, "reason": "r"}],
                                   "2026-07-01")
                v = L4.last_verdict("MU")
                self.assertEqual(v["action"], "TRIM")
                self.assertEqual(v["stop_loss"], 900.0)
                self.assertEqual(L4.stale_days("MU", "2026-07-06"), 5)
                # a failed result is not stored as a verdict
                L4.mark_researched([{"ticker": "NVDA", "ok": False, "reason": "boom"}], "2026-07-01")
                self.assertIsNone(L4.last_verdict("NVDA"))
            finally:
                conf.COVERAGE_PATH = orig

    def test_legacy_date_string_entry_tolerated(self):
        # old ledger format was {ticker: "YYYY-MM-DD"}; stale_days must still work
        cov = {"OLD": "2026-07-01"}
        self.assertEqual(L4.stale_days("OLD", "2026-07-04", cov=cov), 3)
        self.assertIsNone(L4.last_verdict("OLD", cov=cov))


if __name__ == "__main__":
    unittest.main()
