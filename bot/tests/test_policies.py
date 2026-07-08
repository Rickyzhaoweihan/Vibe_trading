#!/usr/bin/env python3
"""Unit tests for policies.py — Tier 1 deterministic policy library. No network."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import policies as P


def regime(label="RISK_ON_TREND", **feats):
    base = {"above_200sma": True, "golden_cross": True, "momentum_63d": 0.1}
    base.update(feats)
    return {"label": label, "features": base}


def bars(closes):
    return {"closes": closes, "highs": [c * 1.01 for c in closes],
            "lows": [c * 0.99 for c in closes]}


def ramp(start, step, n):
    return [start + step * i for i in range(n)]


class TestSma200Trend(unittest.TestCase):
    def test_uptrend_buys_strongest_long(self):
        # TQQQ strongest 20d return, others flat
        market = {
            "TQQQ": bars(ramp(50, 1.0, 30)),     # strong
            "SOXL": bars(ramp(50, 0.1, 30)),     # weak
            "TECL": bars(ramp(50, 0.1, 30)),
            "UPRO": bars(ramp(50, 0.1, 30)),
            "FNGU": bars(ramp(50, 0.1, 30)),
        }
        out = P.sma200_trend(regime(), market, {}, {})
        buys = [i for i in out if i["side"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["ticker"], "TQQQ")
        self.assertEqual(buys[0]["sleeve"], "aggressive")
        self.assertEqual(buys[0]["hold_class"], "swing_lev")

    def test_riskoff_exits_held_longs(self):
        market = {"TQQQ": bars(ramp(80, -1.0, 30))}
        positions = {"TQQQ": {"policy_id": "sma200_trend", "sleeve": "aggressive"}}
        out = P.sma200_trend(
            regime("RISK_OFF_TREND", above_200sma=False, golden_cross=False,
                   momentum_63d=-0.2),
            market, positions, {})
        sells = [i for i in out if i["side"] == "sell"]
        self.assertTrue(any(s["ticker"] == "TQQQ" for s in sells))

    def test_rotation_sells_laggard_when_leader_changes(self):
        market = {
            "TQQQ": bars(ramp(50, 0.1, 30)),     # now weak
            "SOXL": bars(ramp(50, 1.0, 30)),     # now leader
            "TECL": bars(ramp(50, 0.1, 30)),
            "UPRO": bars(ramp(50, 0.1, 30)),
            "FNGU": bars(ramp(50, 0.1, 30)),
        }
        positions = {"TQQQ": {"policy_id": "sma200_trend"}}
        out = P.sma200_trend(regime(), market, positions, {})
        self.assertTrue(any(i["side"] == "sell" and i["ticker"] == "TQQQ" for i in out))
        self.assertTrue(any(i["side"] == "buy" and i["ticker"] == "SOXL" for i in out))


class TestRsi2(unittest.TestCase):
    def _oversold_bars(self):
        # uptrend then a sharp 3-day drop -> RSI(2) very low
        closes = ramp(80, 0.5, 40) + [99, 97, 90]
        return bars(closes)

    def test_buys_oversold_dip_in_uptrend(self):
        market = {"TQQQ": self._oversold_bars()}
        out = P.rsi2_meanrev(regime(), market, {}, {})
        buys = [i for i in out if i["side"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["ticker"], "TQQQ")

    def test_no_buy_when_trend_down(self):
        market = {"TQQQ": self._oversold_bars()}
        out = P.rsi2_meanrev(regime(above_200sma=False), market, {}, {})
        self.assertEqual([i for i in out if i["side"] == "buy"], [])

    def test_exit_on_bounce(self):
        # rising into overbought while held -> sell
        market = {"TQQQ": bars(ramp(80, 1.0, 40))}
        positions = {"TQQQ": {"policy_id": "rsi2_meanrev"}}
        out = P.rsi2_meanrev(regime(), market, positions, {})
        self.assertTrue(any(i["side"] == "sell" and i["ticker"] == "TQQQ" for i in out))


class TestDualMomentum(unittest.TestCase):
    def test_buys_top_momentum_when_positive(self):
        market = {
            "TQQQ": bars(ramp(50, 1.0, 80)),     # strongest 63d
            "SOXL": bars(ramp(50, 0.2, 80)),
            "TECL": bars(ramp(50, 0.2, 80)),
            "UPRO": bars(ramp(50, 0.2, 80)),
            "FNGU": bars(ramp(50, 0.2, 80)),
        }
        out = P.dual_momentum(regime(), market, {}, {})
        buys = [i for i in out if i["side"] == "buy"]
        self.assertEqual(buys[0]["ticker"], "TQQQ")
        self.assertEqual(buys[0]["policy_id"], "dual_momentum")

    def test_exits_when_absolute_momentum_negative(self):
        market = {s: bars(ramp(120, -0.5, 80)) for s in P.LONG_LEV}  # all falling
        positions = {"TQQQ": {"policy_id": "dual_momentum"}}
        out = P.dual_momentum(regime("RISK_OFF_TREND"), market, positions, {})
        self.assertTrue(any(i["side"] == "sell" and i["ticker"] == "TQQQ" for i in out))
        self.assertFalse(any(i["side"] == "buy" for i in out))


class TestOrchestrator(unittest.TestCase):
    def test_weights_and_aggressiveness_scale_buys(self):
        market = {s: bars(ramp(50, (1.0 if s == "TQQQ" else 0.1), 30)) for s in P.LONG_LEV}
        routing = {"active_policies": ["sma200_trend"],
                   "weights": {"sma200_trend": 0.5}, "aggressiveness": 1.0}
        out = P.evaluate(routing, regime(), market, {}, {})
        buy = [i for i in out if i["side"] == "buy"][0]
        # base 0.25 * weight 0.5 * aggr 1.0 = 0.125
        self.assertAlmostEqual(buy["target_frac"], 0.125, places=3)

    def test_disabled_policy_skipped(self):
        market = {s: bars(ramp(50, 1.0, 30)) for s in P.LONG_LEV}
        routing = {"active_policies": ["sma200_trend"]}
        out = P.evaluate(routing, regime(), market, {},
                         {"sma200_trend": {"enabled": False}})
        self.assertEqual(out, [])

    def test_sell_wins_over_buy_conflict(self):
        # one policy would buy TQQQ, another sells it -> sell wins
        merged = P._merge([
            {"ticker": "TQQQ", "side": "buy", "target_frac": 0.2},
            {"ticker": "TQQQ", "side": "sell", "target_frac": 0.0},
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["side"], "sell")


if __name__ == "__main__":
    unittest.main(verbosity=2)
