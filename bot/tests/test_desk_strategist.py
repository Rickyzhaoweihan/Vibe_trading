#!/usr/bin/env python3
"""Unit tests for desk/strategist.py — JSON parse/guard, action→call mapping,
escalation cap, memory round-trip, and feasibility after deterministic sizing.
No network: the OpenRouter call is monkeypatched."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import conf
import strategist as ST
import synthesize as L6
import portfolio as L5


class TestParse(unittest.TestCase):
    ctx = {"portfolio": {"total_value": 10000}, "cash": 2000}

    def test_plain_json(self):
        obj = ST._parse('{"actions": [], "escalate": []}', self.ctx)
        self.assertEqual(obj["actions"], [])

    def test_fenced_json(self):
        obj = ST._parse('```json\n{"actions": []}\n```', self.ctx)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["cash_stance"], "hold")   # defaulted

    def test_prose_wrapped_json(self):
        obj = ST._parse('Here you go:\n{"narrative": {"thesis": "x"}}\ndone', self.ctx)
        self.assertEqual(obj["narrative"]["thesis"], "x")

    def test_malformed_returns_none(self):
        self.assertIsNone(ST._parse("not json at all", self.ctx))
        self.assertIsNone(ST._parse("", self.ctx))
        self.assertIsNone(ST._parse(None, self.ctx))

    def test_dollar_hallucination_rejected(self):
        # a $ figure far bigger than 1.2x the $12k account must reject the whole obj
        bad = '{"actions": [{"ticker":"NVDA","reason":"buy $500000 worth"}]}'
        self.assertIsNone(ST._parse(bad, self.ctx))


class TestActionsToCalls(unittest.TestCase):
    def test_size_hint_discarded_and_shape(self):
        actions = [{"ticker": "NVDA", "action": "TRIM", "size_hint": "5%",
                    "conviction": "high", "stop_loss": 186.3, "reason": "extended"}]
        calls, stance = ST.actions_to_calls(actions, holdings={"NVDA"})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("size_hint", calls[0])
        self.assertEqual(calls[0]["action"], "TRIM")
        self.assertTrue(calls[0]["held"])
        self.assertEqual(calls[0]["conviction"], "high")

    def test_hedge_ticker_dropped(self):
        # a hedge instrument is owned by the hedge engine — never a stock call
        calls, _ = ST.actions_to_calls(
            [{"ticker": "SQQQ", "action": "BUY"}], holdings=set())
        self.assertEqual(calls, [])

    def test_raise_cash_sets_stance_not_a_call(self):
        calls, stance = ST.actions_to_calls(
            [{"ticker": "", "action": "RAISE_CASH"}], holdings=set())
        self.assertEqual(calls, [])
        self.assertEqual(stance, "raise")

    def test_buy_normalized_to_new_buy_when_not_held(self):
        calls, _ = ST.actions_to_calls(
            [{"ticker": "ARM", "action": "BUY"}], holdings={"NVDA"})
        self.assertEqual(calls[0]["action"], "NEW_BUY")

    def test_new_buy_normalized_to_buy_when_held(self):
        calls, _ = ST.actions_to_calls(
            [{"ticker": "NVDA", "action": "NEW_BUY"}], holdings={"NVDA"})
        self.assertEqual(calls[0]["action"], "BUY")


class TestFeasibilityAfterSizing(unittest.TestCase):
    def test_buy_with_no_cash_drops_to_keep(self):
        # the strategist can name a buy; plan_trades must make it feasible or drop it
        calls, _ = ST.actions_to_calls(
            [{"ticker": "ARM", "action": "BUY", "conviction": "high"}], holdings=set())
        L5.plan_trades(calls, values={}, total=10000, net_target=0.7,
                       current_net=0.8, band="low", prices={"ARM": 150.0}, cash=0.0)
        self.assertEqual(calls[0]["action"], "KEEP")     # unfundable → downgraded
        infeasible = L5.audit_feasibility(calls, {}, 0.0)
        self.assertEqual(infeasible, [])                 # nothing un-executable ships

    def test_trim_bigger_than_position_is_flagged(self):
        calls = [{"ticker": "NVDA", "action": "TRIM", "conviction": "low", "dollars": 5000}]
        # position only worth $1000 → audit must flag it
        infeasible = L5.audit_feasibility(calls, {"NVDA": 1000.0}, 0.0)
        self.assertTrue(any("INFEASIBLE" in w for w in infeasible))


class TestEscalationCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = conf.STRATEGIST_STATE_PATH
        conf.STRATEGIST_STATE_PATH = Path(self._tmp.name) / "strat_state.json"

    def tearDown(self):
        conf.STRATEGIST_STATE_PATH = self._orig
        self._tmp.cleanup()

    def test_cap_and_dedup(self):
        cov = {}   # nothing researched today
        picked = ST.cap_escalations(
            [{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}],
            date="2026-07-14", coverage=cov, cap=2)
        self.assertEqual(picked, ["A", "B"])            # hard cap 2
        # a second call the same day sees A,B already spent → only 0 slots left
        picked2 = ST.cap_escalations(
            [{"ticker": "D"}], date="2026-07-14", coverage=cov, cap=2)
        self.assertEqual(picked2, [])

    def test_already_researched_today_skipped(self):
        cov = {"A": {"date": "2026-07-14"}}             # A done today
        picked = ST.cap_escalations(
            [{"ticker": "A"}, {"ticker": "B"}], date="2026-07-14", coverage=cov, cap=2)
        self.assertEqual(picked, ["B"])

    def test_new_day_resets(self):
        cov = {}
        ST.cap_escalations([{"ticker": "A"}, {"ticker": "B"}],
                           date="2026-07-14", coverage=cov, cap=2)
        picked = ST.cap_escalations([{"ticker": "X"}], date="2026-07-15", coverage=cov, cap=2)
        self.assertEqual(picked, ["X"])


class TestMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = conf.STRATEGIST_MEMORY_PATH
        conf.STRATEGIST_MEMORY_PATH = Path(self._tmp.name) / "mem.md"

    def tearDown(self):
        conf.STRATEGIST_MEMORY_PATH = self._orig
        self._tmp.cleanup()

    def test_seed_when_missing(self):
        self.assertIn("Strategist Memory", ST.load_memory())

    def test_round_trip_and_stamp(self):
        ST.save_memory("## THESES\n- AI-memory core bet", date="2026-07-14")
        mem = ST.load_memory()
        self.assertIn("updated 2026-07-14", mem)
        self.assertIn("AI-memory core bet", mem)

    def test_bounded(self):
        conf.STRATEGIST["memory_max_bytes"] = 200
        ST.save_memory("## THESES\n" + ("- long line of memory text\n" * 200),
                       date="2026-07-14")
        self.assertLessEqual(len(conf.STRATEGIST_MEMORY_PATH.read_text().encode()), 200)
        conf.STRATEGIST["memory_max_bytes"] = 8000

    def test_empty_update_keeps_old(self):
        ST.save_memory("## THESES\n- keep me", date="2026-07-14")
        self.assertFalse(ST.save_memory("", date="2026-07-15"))
        self.assertIn("keep me", ST.load_memory())


class TestRunStrategistMocked(unittest.TestCase):
    def setUp(self):
        self._orig = L6.openrouter_chat
        self._news = ST.NEWS.event_digest
        ST.NEWS.event_digest = lambda *a, **k: ""      # no network in tests
        self._tmp = tempfile.TemporaryDirectory()
        self._om, self._os = conf.STRATEGIST_MEMORY_PATH, conf.STRATEGIST_STATE_PATH
        conf.STRATEGIST_MEMORY_PATH = Path(self._tmp.name) / "mem.md"
        conf.STRATEGIST_STATE_PATH = Path(self._tmp.name) / "state.json"

    def tearDown(self):
        L6.openrouter_chat = self._orig
        ST.NEWS.event_digest = self._news
        conf.STRATEGIST_MEMORY_PATH, conf.STRATEGIST_STATE_PATH = self._om, self._os
        self._tmp.cleanup()

    def _ctx(self):
        return {"date": "2026-07-14", "mode": "preopen", "cash": 2000,
                "portfolio": {"total_value": 10000, "positions": [{"symbol": "NVDA"}]},
                "calls": [{"ticker": "NVDA", "action": "KEEP", "conviction": "low"}],
                "macro": {}, "unwind": {}, "ideas": {"equity": []},
                "activity": [], "review": {}, "pulse": {}, "hedge": {}}

    def test_happy_path(self):
        payload = json.dumps({"narrative": {"thesis": "crowded book"},
                              "actions": [{"ticker": "NVDA", "action": "KEEP"}],
                              "escalate": [], "memory_update": "## THESES\n- x"})
        L6.openrouter_chat = lambda *a, **k: payload
        res = ST.run_strategist(self._ctx(), mode="preopen", date="2026-07-14")
        self.assertIsNotNone(res)
        self.assertEqual(res["narrative"]["thesis"], "crowded book")

    def test_retry_then_success(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            return "garbage" if calls["n"] == 1 else '{"actions": []}'
        L6.openrouter_chat = fake
        res = ST.run_strategist(self._ctx(), mode="preopen", date="2026-07-14")
        self.assertIsNotNone(res)
        self.assertEqual(calls["n"], 2)                  # retried once

    def test_total_failure_returns_none(self):
        L6.openrouter_chat = lambda *a, **k: "still not json"
        res = ST.run_strategist(self._ctx(), mode="preopen", date="2026-07-14")
        self.assertIsNone(res)                           # caller falls back to carried calls


if __name__ == "__main__":
    unittest.main()
