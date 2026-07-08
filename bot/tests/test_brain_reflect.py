#!/usr/bin/env python3
"""Unit tests for brain.py (deterministic routing fallback) and reflect.py
(P&L attribution + safety-railed proposals). No network."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain
import reflect
import regime as rg


def regime_out(label, **feats):
    return {"label": label, "features": feats}


class TestDefaultRoute(unittest.TestCase):
    def test_risk_on_runs_both_full(self):
        r = brain.default_route(regime_out(rg.RISK_ON_TREND))
        self.assertEqual(set(r["active_policies"]), {"sma200_trend", "rsi2_meanrev"})
        self.assertEqual(r["aggressiveness"], 1.0)

    def test_high_vol_cuts_aggressiveness(self):
        r = brain.default_route(regime_out(rg.HIGH_VOL_CHOP))
        self.assertLess(r["aggressiveness"], 0.6)
        self.assertEqual(r["active_policies"], ["rsi2_meanrev"])

    def test_risk_off_keeps_only_trend_filter(self):
        r = brain.default_route(regime_out(rg.RISK_OFF_TREND))
        self.assertEqual(r["active_policies"], ["sma200_trend"])

    def test_disabled_policy_excluded(self):
        r = brain.default_route(regime_out(rg.RISK_ON_TREND),
                                {"rsi2_meanrev": {"enabled": False}})
        self.assertNotIn("rsi2_meanrev", r["active_policies"])


class TestSanitize(unittest.TestCase):
    def test_drops_unknown_policy(self):
        r = brain.sanitize({"active_policies": ["made_up", "sma200_trend"],
                            "weights": {"sma200_trend": 1.0}, "aggressiveness": 1.0})
        self.assertEqual(r["active_policies"], ["sma200_trend"])

    def test_clamps_aggressiveness(self):
        r = brain.sanitize({"active_policies": ["sma200_trend"],
                            "weights": {}, "aggressiveness": 99})
        self.assertLessEqual(r["aggressiveness"], 1.5)

    def test_empty_returns_fallback(self):
        fb = {"active_policies": ["rsi2_meanrev"], "weights": {"rsi2_meanrev": 1.0},
              "aggressiveness": 0.5}
        r = brain.sanitize({"active_policies": []}, fallback=fb)
        self.assertEqual(r, fb)


def trade(side, policy_id, pnl=None):
    return {"side": side, "policy_id": policy_id, "pnl": pnl}


class TestAttribution(unittest.TestCase):
    def test_win_rate_and_pnl(self):
        trades = [
            trade("buy", "rsi2_meanrev"),
            trade("sell", "rsi2_meanrev", pnl=5.0),
            trade("buy", "rsi2_meanrev"),
            trade("sell", "rsi2_meanrev", pnl=-2.0),
        ]
        stats = reflect.compute_attribution(trades)["rsi2_meanrev"]
        self.assertEqual(stats["n_closed"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertAlmostEqual(stats["total_pnl"], 3.0)
        self.assertAlmostEqual(stats["win_rate"], 0.5)

    def test_untagged_ignored(self):
        stats = reflect.compute_attribution([{"side": "buy"}])
        self.assertEqual(stats, {})


class TestProposals(unittest.TestCase):
    def _cfg(self, w=1.0):
        return {"rsi2_meanrev": {"weight": w}}

    def test_no_change_below_min_trades(self):
        stats = {"rsi2_meanrev": {"n_closed": 3, "win_rate": 0.9, "avg_pnl": 5.0}}
        self.assertEqual(reflect.propose_updates(stats, self._cfg()), {})

    def test_raise_strong_policy(self):
        stats = {"rsi2_meanrev": {"n_closed": 20, "win_rate": 0.7, "avg_pnl": 4.0}}
        p = reflect.propose_updates(stats, self._cfg(1.0))
        self.assertIn("rsi2_meanrev", p)
        self.assertGreater(p["rsi2_meanrev"]["new_weight"], 1.0)

    def test_cut_weak_policy(self):
        stats = {"rsi2_meanrev": {"n_closed": 20, "win_rate": 0.3, "avg_pnl": -1.0}}
        p = reflect.propose_updates(stats, self._cfg(1.0))
        self.assertLess(p["rsi2_meanrev"]["new_weight"], 1.0)

    def test_delta_and_bounds_capped(self):
        stats = {"rsi2_meanrev": {"n_closed": 99, "win_rate": 0.99, "avg_pnl": 50.0}}
        p = reflect.propose_updates(stats, self._cfg(1.95))
        # capped by both max_delta and the 2.0 upper bound
        self.assertLessEqual(p["rsi2_meanrev"]["new_weight"], 2.0)

    def test_apply_records_change_log(self):
        cfg = {"rsi2_meanrev": {"weight": 1.0, "change_log": []}}
        proposals = {"rsi2_meanrev": {"old_weight": 1.0, "new_weight": 1.2, "reason": "x"}}
        out = reflect.apply_updates(cfg, proposals, "2026-06-14T00:00:00")
        self.assertEqual(out["rsi2_meanrev"]["weight"], 1.2)
        self.assertEqual(len(out["rsi2_meanrev"]["change_log"]), 1)
        # original cfg unmutated (pure)
        self.assertEqual(cfg["rsi2_meanrev"]["weight"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
