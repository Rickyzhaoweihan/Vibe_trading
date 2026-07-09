#!/usr/bin/env python3
"""Unit tests for desk/portfolio.py — exposure / concentration / actions. No network."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import portfolio as L5


POSITIONS = [
    {"symbol": "NVDA", "quantity": 10},   # ai_semis
    {"symbol": "MU", "quantity": 10},     # memory
    {"symbol": "MSFT", "quantity": 10},   # megacap_tech
    {"symbol": "VOO", "quantity": 10},    # index_beta
    {"symbol": "TSLA", "quantity": 10},   # tail
]
PRICES = {"NVDA": 100, "MU": 100, "MSFT": 100, "VOO": 100, "TSLA": 100}


class TestPortfolio(unittest.TestCase):
    def test_position_values(self):
        v = L5.position_values(POSITIONS, PRICES)
        self.assertEqual(v["NVDA"], 1000)
        self.assertEqual(len(v), 5)

    def test_position_values_skips_missing_price(self):
        v = L5.position_values(POSITIONS, {"NVDA": 100})
        self.assertEqual(set(v), {"NVDA"})

    def test_cluster_pct_sums_to_one(self):
        v = L5.position_values(POSITIONS, PRICES)
        cl = L5.cluster_exposure(v)
        self.assertAlmostEqual(sum(c["pct"] for c in cl.values()), 1.0, places=6)

    def test_crowding_share(self):
        # NVDA+MU+MSFT are momentum clusters => 3/5
        v = L5.position_values(POSITIONS, PRICES)
        self.assertAlmostEqual(L5.crowding_share(v), 0.6, places=6)

    def test_concentration(self):
        v = L5.position_values(POSITIONS, PRICES)
        c = L5.concentration(v)
        self.assertEqual(c["positions"], 5)
        self.assertAlmostEqual(c["top1"], 0.2, places=6)

    def test_defensive_actions_reduce_when_over(self):
        acts = L5.defensive_actions(unwind_band="high", crowding=0.6,
                                    net_target=0.5, current_net=1.0,
                                    hedges=["cash", "gold"])
        joined = " ".join(acts)
        self.assertIn("Reduce net exposure", joined)
        self.assertIn("Unwind risk HIGH", joined)
        self.assertIn("Concentration", joined)

    def test_plan_trades_sizes_and_times(self):
        calls = [
            {"ticker": "SNDK", "action": "SELL"},
            {"ticker": "MU", "action": "TRIM"},
            {"ticker": "AMD", "action": "NEW_BUY"},
            {"ticker": "NVDA", "action": "KEEP"},
        ]
        values = {"SNDK": 1400.0, "MU": 1000.0}
        cash = L5.plan_trades(calls, values=values, total=10000.0,
                              net_target=0.9, current_net=1.0, band="elevated",
                              prices={"AMD": 100.0})
        by = {c["ticker"]: c for c in calls}
        self.assertEqual(by["SNDK"]["dollars"], 1400)            # full position
        self.assertEqual(by["MU"]["dollars"], 330)              # 33% trim (elevated band)
        self.assertEqual(by["AMD"]["dollars"], 500)             # 5% of book
        self.assertEqual(by["AMD"]["entry_zone"], 97.0)         # dip = -3%
        self.assertIn("dip", by["AMD"]["when"])
        self.assertIsNone(by["NVDA"]["dollars"])                # KEEP: no size
        self.assertEqual(cash, 1000)                            # (1.0-0.9)*10000

    def test_plan_trades_buys_funded_by_conviction_order(self):
        # highest conviction funds first regardless of list order; the starved buy
        # is demoted to KEEP rather than left as an unfundable call
        calls = [{"ticker": "LOW", "action": "NEW_BUY", "conviction": "low"},
                 {"ticker": "HIGH", "action": "NEW_BUY", "conviction": "high"}]
        L5.plan_trades(calls, values={}, total=10000.0, net_target=0.9,
                       current_net=0.9, band="low", prices={"LOW": 100.0, "HIGH": 100.0},
                       cash=500.0)   # only room for one 5% ($500) buy
        by = {c["ticker"]: c for c in calls}
        self.assertEqual(by["HIGH"]["dollars"], 500)            # high conviction funded
        self.assertEqual(by["LOW"]["action"], "KEEP")           # starved buy demoted
        self.assertIn("现金不足", by["LOW"]["reason"])

    def test_plan_trades_buy_add_beats_new_buy_at_equal_conviction(self):
        calls = [{"ticker": "NEW", "action": "NEW_BUY"},        # no conviction on either
                 {"ticker": "ADD", "action": "BUY"}]
        L5.plan_trades(calls, values={}, total=10000.0, net_target=0.9,
                       current_net=0.9, band="low", prices={"NEW": 100.0, "ADD": 100.0},
                       cash=300.0)   # room for the 3% add ($300) only
        by = {c["ticker"]: c for c in calls}
        self.assertEqual(by["ADD"]["dollars"], 300)             # add to winner funded first
        self.assertEqual(by["NEW"]["action"], "KEEP")

    def test_plan_trades_share_counts(self):
        calls = [{"ticker": "MU", "action": "TRIM"}, {"ticker": "AMD", "action": "NEW_BUY"}]
        L5.plan_trades(calls, values={"MU": 1000.0}, total=10000.0, net_target=0.9,
                       current_net=0.9, band="low", prices={"MU": 100.0, "AMD": 100.0},
                       cash=5000.0)
        by = {c["ticker"]: c for c in calls}
        self.assertAlmostEqual(by["MU"]["shares"], 2.5, places=3)       # $250 trim / $100
        self.assertAlmostEqual(by["AMD"]["shares"], 500 / 97.0, places=3)  # at the dip zone

    def test_plan_trades_urgent_when_high(self):
        calls = [{"ticker": "MU", "action": "SELL"}]
        L5.plan_trades(calls, values={"MU": 500.0}, total=5000.0, net_target=0.9,
                       current_net=1.0, band="high", prices={})
        self.assertIn("now", calls[0]["when"])

    def test_defensive_actions_quiet_when_fine(self):
        acts = L5.defensive_actions(unwind_band="low", crowding=0.2,
                                    net_target=0.9, current_net=0.9, hedges=[])
        self.assertEqual(len(acts), 1)
        self.assertIn("No portfolio action", acts[0])


class TestFeasibilityAudit(unittest.TestCase):
    def test_all_feasible_is_clean(self):
        calls = [{"action": "TRIM", "ticker": "MU", "dollars": 200},
                 {"action": "SELL", "ticker": "SPCX", "dollars": 150},
                 {"action": "BUY", "ticker": "AMD", "dollars": 300}]
        vals = {"MU": 800, "SPCX": 150}
        self.assertEqual(L5.audit_feasibility(calls, vals, cash=500), [])

    def test_trim_bigger_than_position_flagged(self):
        calls = [{"action": "TRIM", "ticker": "MRVL", "dollars": 378}]
        w = L5.audit_feasibility(calls, {"MRVL": 245}, cash=1000)
        self.assertTrue(w and w[0].startswith("INFEASIBLE") and "MRVL" in w[0])

    def test_sell_exceeding_holding_flagged(self):
        w = L5.audit_feasibility([{"action": "SELL", "ticker": "X", "dollars": 500}],
                                 {"X": 100}, cash=0)
        self.assertTrue(w and "INFEASIBLE" in w[0])

    def test_buys_exceeding_cash_flagged(self):
        calls = [{"action": "BUY", "ticker": "A", "dollars": 400},
                 {"action": "NEW_BUY", "ticker": "B", "dollars": 400}]
        w = L5.audit_feasibility(calls, {}, cash=500)   # 400+400 > 500
        self.assertTrue(any("exceeds remaining cash" in x for x in w))

    def test_keep_and_null_ignored(self):
        calls = [{"action": "KEEP", "ticker": "A"}, {"action": "TRIM", "ticker": "B", "dollars": None}]
        self.assertEqual(L5.audit_feasibility(calls, {}, cash=0), [])


class TestHedgeCashCap(unittest.TestCase):
    def test_hedge_capped_by_cash(self):
        h = L5.hedge_plan(equity=100000, regime_label="RISK_ON_TREND", unwind_band="low",
                          current_net=0.9, net_target=0.9, crowding=0.4, base_hedge=0.05,
                          cash=1200)   # 5% of 100k = $5000 notional, but only $1200 cash
        psq = next(o for o in h["options"] if o["ticker"] == "PSQ")
        self.assertEqual(psq["capital"], 1200)          # capped at cash
        self.assertTrue(psq["cash_capped"])

    def test_hedge_uncapped_when_cash_ample(self):
        h = L5.hedge_plan(equity=10000, regime_label="RISK_ON_TREND", unwind_band="low",
                          current_net=0.9, net_target=0.9, crowding=0.4, base_hedge=0.05,
                          cash=5000)   # $500 notional << $5000 cash
        psq = next(o for o in h["options"] if o["ticker"] == "PSQ")
        self.assertEqual(psq["capital"], 500)
        self.assertFalse(psq["cash_capped"])


class TestHedgePlan(unittest.TestCase):
    def test_standing_floor_in_calm_tape(self):
        h = L5.hedge_plan(equity=10000, regime_label="RISK_ON_TREND",
                          unwind_band="low", current_net=0.85, net_target=0.90,
                          crowding=0.4, base_hedge=0.05)
        self.assertEqual(h["target_pct"], 0.05)           # just the floor
        self.assertEqual(h["notional"], 500)
        self.assertEqual(h["recommend"], "PSQ")           # 1x holdable in calm tape
        self.assertIn("insurance", h["urgency"])

    def test_unconfirmed_falls_back_to_1x(self):
        # risk-off but the confirm didn't run (None) => don't deploy leverage blind
        h = L5.hedge_plan(equity=10000, regime_label="RISK_OFF_TREND",
                          unwind_band="high", current_net=1.0, net_target=0.30,
                          crowding=0.7, base_hedge=0.05, confirm_rating=None)
        self.assertEqual(h["target_pct"], 0.40)           # capped at 40%
        self.assertEqual(h["urgency"], "now")
        self.assertEqual(h["recommend"], "PSQ")
        self.assertFalse(h["confirmed"])

    def test_sqqq_led_when_bearish_and_risk(self):
        h = L5.hedge_plan(equity=10000, regime_label="RISK_OFF_TREND",
                          unwind_band="high", current_net=1.0, net_target=0.30,
                          crowding=0.7, base_hedge=0.05, confirm_rating="Underweight")
        self.assertEqual(h["recommend"], "SQQQ")
        self.assertTrue(h["confirmed"])

    def test_sqqq_reachable_on_cautious_read_when_risk_off(self):
        # the recalibration: a NEUTRAL (Hold) read in a clearly risk-off tape is enough
        h = L5.hedge_plan(equity=10000, regime_label="HIGH_VOL_CHOP",
                          unwind_band="high", current_net=1.0, net_target=0.5,
                          crowding=0.7, base_hedge=0.05, confirm_rating="Hold")
        self.assertEqual(h["recommend"], "SQQQ")

    def test_sqqq_reachable_on_bearish_at_merely_elevated(self):
        # bearish research + only elevated unwind still green-lights (early hedge)
        h = L5.hedge_plan(equity=10000, regime_label="NEUTRAL",
                          unwind_band="elevated", current_net=0.9, net_target=0.7,
                          crowding=0.5, base_hedge=0.05, confirm_rating="Sell")
        self.assertEqual(h["recommend"], "SQQQ")

    def test_sqqq_vetoed_when_research_bullish(self):
        h = L5.hedge_plan(equity=10000, regime_label="HIGH_VOL_CHOP",
                          unwind_band="high", current_net=1.0, net_target=0.5,
                          crowding=0.7, base_hedge=0.05, confirm_rating="Overweight")
        self.assertEqual(h["recommend"], "PSQ")           # never -3x-short a buy-rated tape

    def test_neutral_at_only_elevated_holds_1x(self):
        # cautious read + only mild risk = not yet; stay in the -1x
        h = L5.hedge_plan(equity=10000, regime_label="NEUTRAL",
                          unwind_band="elevated", current_net=0.8, net_target=0.7,
                          crowding=0.5, base_hedge=0.05, confirm_rating="Hold")
        self.assertEqual(h["recommend"], "PSQ")

    def test_needs_confirm_gate_fires_at_elevated(self):
        self.assertTrue(L5.needs_sqqq_confirm("RISK_OFF_TREND", "low"))
        self.assertTrue(L5.needs_sqqq_confirm("NEUTRAL", "elevated"))   # early check
        self.assertTrue(L5.needs_sqqq_confirm("NEUTRAL", "high"))
        self.assertFalse(L5.needs_sqqq_confirm("RISK_ON_TREND", "low"))

    def test_3x_uses_less_capital_for_same_notional(self):
        h = L5.hedge_plan(equity=10000, regime_label="HIGH_VOL_CHOP",
                          unwind_band="elevated", current_net=0.8, net_target=0.5,
                          crowding=0.5, base_hedge=0.05)
        opts = {o["ticker"]: o for o in h["options"]}
        self.assertEqual(opts["PSQ"]["capital"], h["notional"])           # 1x: capital == notional
        self.assertEqual(opts["SQQQ"]["capital"], round(h["notional"] / 3))  # 3x: a third
        self.assertEqual(opts["PSQ"]["neutralizes"], opts["SQQQ"]["neutralizes"])  # same hedge


class TestDiffPositions(unittest.TestCase):
    def _doc(self, items):
        return {"positions": [{"symbol": s, "quantity": q} for s, q in items]}

    def test_no_change_is_empty(self):
        a = self._doc([("NVDA", 3.0), ("MU", 1.0)])
        self.assertEqual(L5.diff_positions(a, a), [])

    def test_detects_new_and_closed(self):
        prev = self._doc([("NVDA", 3.0), ("SNDK", 0.7)])
        curr = self._doc([("NVDA", 3.0), ("DELL", 0.5)])
        d = {x["symbol"]: x for x in L5.diff_positions(prev, curr)}
        self.assertEqual(d["DELL"]["kind"], "NEW")
        self.assertEqual(d["SNDK"]["kind"], "CLOSED")
        self.assertNotIn("NVDA", d)

    def test_detects_added_and_reduced(self):
        prev = self._doc([("MU", 1.0), ("MRVL", 3.0)])
        curr = self._doc([("MU", 1.5), ("MRVL", 2.0)])
        d = {x["symbol"]: x for x in L5.diff_positions(prev, curr)}
        self.assertEqual(d["MU"]["kind"], "ADDED")
        self.assertAlmostEqual(d["MU"]["delta"], 0.5, places=6)
        self.assertEqual(d["MRVL"]["kind"], "REDUCED")
        self.assertAlmostEqual(d["MRVL"]["delta"], -1.0, places=6)

    def test_tiny_delta_ignored(self):
        prev = self._doc([("NVDA", 3.0000001)])
        curr = self._doc([("NVDA", 3.0)])
        self.assertEqual(L5.diff_positions(prev, curr), [])


if __name__ == "__main__":
    unittest.main()
