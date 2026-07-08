#!/usr/bin/env python3
"""Unit tests for desk/monitor.py triggers + journal scoring + digest. No network."""

import json
import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT / "desk"))
sys.path.insert(0, str(BOT))
import monitor as M
import journal as J
import synthesize as S


class TestTriggers(unittest.TestCase):
    def test_pct_move(self):
        self.assertAlmostEqual(M.pct_move(110, 100), 0.10)
        self.assertIsNone(M.pct_move(None, 100))
        self.assertIsNone(M.pct_move(110, 0))

    def test_name_trigger_fires_on_big_move(self):
        ind = {"last": 105.0, "prev_close": 100.0}      # +5% > 4% threshold
        fired = M.name_triggers("NVDA", ind)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["kind"], "move")

    def test_name_trigger_quiet_on_small_move(self):
        self.assertEqual(M.name_triggers("NVDA", {"last": 101.0, "prev_close": 100.0}), [])

    def test_macro_trigger_rates_jump(self):
        market = {"^TNX": {"closes": [4.0, 4.0], "highs": [], "lows": []},
                  "BTC-USD": {"closes": [1, 1]}}
        # prev_close=4.0, last=4.10 -> +10bp >= 8bp
        market["^TNX"]["closes"] = [4.0, 4.10]
        fired = M.macro_triggers(market, vix=15.0)
        kinds = {f["kind"] for f in fired}
        self.assertIn("rates", kinds)

    def test_macro_trigger_vix(self):
        fired = M.macro_triggers({"^TNX": {"closes": [4.0, 4.0]}}, vix=25.0)
        self.assertIn("vix", {f["kind"] for f in fired})

    def test_plan_entry_fires_at_zone(self):
        plan = [{"ticker": "AMD", "action": "NEW_BUY", "entry_zone": 100.0, "dollars": 500}]
        market = {"AMD": {"closes": [120, 100]}}            # last 100 <= zone
        fired = M.plan_triggers(plan, market)
        self.assertTrue(any(f["kind"] == "entry:AMD" for f in fired))

    def test_plan_entry_quiet_above_zone(self):
        plan = [{"ticker": "AMD", "action": "NEW_BUY", "entry_zone": 100.0}]
        market = {"AMD": {"closes": [120, 115]}}            # last 115 > zone
        self.assertEqual(M.plan_triggers(plan, market), [])

    def test_plan_stop_fires(self):
        plan = [{"ticker": "MU", "action": "BUY", "stop": 90.0}]
        market = {"MU": {"closes": [100, 88]}}              # last 88 <= stop
        self.assertTrue(any(f["kind"] == "stop:MU" for f in M.plan_triggers(plan, market)))

    def test_plan_exit_on_gap_up(self):
        plan = [{"ticker": "SNDK", "action": "TRIM", "dollars": 300}]
        market = {"SNDK": {"closes": [100, 104]}}           # +4% strength
        # exits require POSITIVE knowledge the name is still held
        self.assertTrue(any(f["kind"] == "exit:SNDK"
                            for f in M.plan_triggers(plan, market, held={"SNDK"})))

    def test_plan_exit_suppressed_when_book_unknown(self):
        # book unreadable (held=None) must NOT fire a sell for a maybe-sold name
        plan = [{"ticker": "SNDK", "action": "TRIM", "dollars": 300}]
        market = {"SNDK": {"closes": [100, 104]}}
        self.assertEqual([f for f in M.plan_triggers(plan, market, held=None)
                          if f["kind"].startswith("exit")], [])

    def test_plan_stop_fires_even_when_book_unknown(self):
        # protective stop still fires under an unknown book (safety over silence)
        plan = [{"ticker": "MU", "action": "TRIM", "stop": 90.0}]
        market = {"MU": {"closes": [100, 88]}}
        self.assertTrue(any(f["kind"] == "stop:MU"
                            for f in M.plan_triggers(plan, market, held=None)))

    def test_unwind_trigger_high_only(self):
        self.assertEqual(M.unwind_trigger({"band": "elevated", "score": 50}), [])
        self.assertEqual(len(M.unwind_trigger({"band": "high", "score": 80, "reasons": ["x"]})), 1)

    def test_held_leveraged_hedges(self):
        self.assertEqual(M.held_leveraged_hedges(["NVDA", "SQQQ", "PSQ"]), ["SQQQ"])  # PSQ is holdable
        self.assertEqual(M.held_leveraged_hedges(["NVDA", "QQQ"]), [])

    def test_hedge_stop_fires_on_loss(self):
        pos = {"positions": [{"symbol": "SQQQ", "quantity": 10, "average_buy_price": 40.0}]}
        market = {"SQQQ": {"closes": [40, 36], "last": 36.0, "prev_close": 40.0}}  # -10% vs cost
        self.assertTrue(any(f["kind"] == "hedgestop:SQQQ" for f in M.hedge_triggers(pos, market)))

    def test_hedge_quiet_within_stop(self):
        pos = {"positions": [{"symbol": "SQQQ", "quantity": 10, "average_buy_price": 40.0}]}
        market = {"SQQQ": {"closes": [40, 39], "last": 39.0, "prev_close": 40.0}}  # -2.5%, inside stop
        self.assertEqual([f for f in M.hedge_triggers(pos, market) if f["kind"].startswith("hedgestop")], [])

    def test_hedge_underlying_reversal_warns(self):
        pos = {"positions": [{"symbol": "SQQQ", "quantity": 10, "average_buy_price": 40.0}]}
        market = {"SQQQ": {"closes": [40, 40], "last": 40.0, "prev_close": 40.0},
                  "QQQ": {"closes": [700, 715], "last": 715.0, "prev_close": 700.0}}  # +2.1% underlying
        self.assertTrue(any(f["kind"] == "hedgerev:SQQQ" for f in M.hedge_triggers(pos, market)))

    def test_hedge_no_position_no_trigger(self):
        self.assertEqual(M.hedge_triggers({"positions": [{"symbol": "NVDA", "quantity": 1}]}, {}), [])

    def test_plan_exit_and_stop_suppressed_after_user_sold(self):
        # the user sold QQQ/TSM mid-session: exit/stop alerts must stop firing
        plan = [{"ticker": "QQQ", "action": "TRIM", "dollars": 300, "stop": 700.0},
                {"ticker": "TSM", "action": "SELL", "stop": 430.0}]
        market = {"QQQ": {"closes": [680, 695]},           # below stop AND +2.2% strength
                  "TSM": {"closes": [420, 429]}}           # below stop
        fired = M.plan_triggers(plan, market, held={"NVDA", "MSFT"})
        self.assertEqual(fired, [])

    def test_plan_exit_still_fires_while_held(self):
        plan = [{"ticker": "QQQ", "action": "TRIM", "dollars": 300}]
        market = {"QQQ": {"closes": [680, 695]}}           # +2.2% strength
        fired = M.plan_triggers(plan, market, held={"QQQ"})
        self.assertTrue(any(f["kind"] == "exit:QQQ" for f in fired))

    def test_new_buy_entry_quiet_once_bought(self):
        plan = [{"ticker": "ANET", "action": "NEW_BUY", "entry_zone": 165.0}]
        market = {"ANET": {"closes": [170, 164.0]}}        # in the entry zone
        self.assertEqual(M.plan_triggers(plan, market, held={"ANET"}), [])
        self.assertTrue(M.plan_triggers(plan, market, held={"NVDA"}))   # not yet bought => fires

    def test_buy_add_entry_fires_even_when_held(self):
        plan = [{"ticker": "NVDA", "action": "BUY", "entry_zone": 190.0}]
        market = {"NVDA": {"closes": [195, 189.5]}}
        fired = M.plan_triggers(plan, market, held={"NVDA"})
        self.assertTrue(any(f["kind"] == "entry:NVDA" for f in fired))

    def test_plan_near_entry_heads_up(self):
        plan = [{"ticker": "AMD", "action": "BUY", "entry_zone": 100.0, "dollars": 500}]
        market = {"AMD": {"closes": [104, 101.0]}}          # 1% above zone: near, not in
        fired = M.plan_triggers(plan, market, near_pct=0.012)
        self.assertTrue(any(f["kind"] == "near:AMD" for f in fired))
        self.assertFalse(any(f["kind"] == "entry:AMD" for f in fired))

    def test_refire_same_bucket_dedupes_next_bucket_fires(self):
        state = {"date": "2026-07-01", "fired": []}
        trig = [{"kind": "entry:TSM", "ticker": "TSM", "detail": "x"}]
        first = M._fresh(list(trig), state, "2026-07-01", now_min=600, refire_minutes=90)
        again = M._fresh(list(trig), state, "2026-07-01", now_min=610, refire_minutes=90)   # same 90-min bucket
        later = M._fresh(list(trig), state, "2026-07-01", now_min=700, refire_minutes=90)   # next bucket
        self.assertEqual(len(first), 1)
        self.assertEqual(len(again), 0)
        self.assertEqual(len(later), 1)

    def test_informational_kind_stays_once_per_day(self):
        state = {"date": "2026-07-01", "fired": []}
        trig = [{"kind": "move", "ticker": "NVDA", "detail": "x"}]
        self.assertEqual(len(M._fresh(list(trig), state, "2026-07-01", now_min=600)), 1)
        self.assertEqual(len(M._fresh(list(trig), state, "2026-07-01", now_min=700)), 0)

    def test_dedupe_fresh(self):
        state = {"date": "2026-06-24", "fired": []}
        trigs = [{"kind": "move", "ticker": "NVDA", "detail": "x"}]
        first = M._fresh(trigs, state, "2026-06-24")
        self.assertEqual(len(first), 1)
        second = M._fresh(trigs, state, "2026-06-24")    # same day, same key
        self.assertEqual(second, [])
        # new day resets
        third = M._fresh(trigs, state, "2026-06-25")
        self.assertEqual(len(third), 1)


class TestJournal(unittest.TestCase):
    def test_score_long_win(self):
        r = J.score_call({"action": "BUY", "entry": 100}, 110)
        self.assertAlmostEqual(r["return"], 0.10)

    def test_score_sell_avoided_downside(self):
        # sold at 100, it fell to 90 -> a win for the exit call
        r = J.score_call({"action": "SELL", "entry": 100}, 90)
        self.assertAlmostEqual(r["return"], 0.10)

    def test_score_stop_hit(self):
        r = J.score_call({"action": "BUY", "entry": 100, "stop": 95}, 94)
        self.assertEqual(r["status"], "stop_hit")

    def test_score_open_without_entry(self):
        self.assertEqual(J.score_call({"action": "BUY"}, 110)["status"], "open")

    def test_log_and_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.jsonl"
            n = J.log_calls([{"ticker": "NVDA", "action": "BUY"},
                             {"ticker": "X", "action": "KEEP"}], date="2026-06-24", path=p)
            self.assertEqual(n, 1)                       # KEEP not tracked
            self.assertEqual(len(J.load_journal(p)), 1)

    def test_episode_dedup_no_double_log(self):
        # the same BUY re-emitted daily is ONE episode, logged once
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.jsonl"
            J.log_calls([{"ticker": "NVDA", "action": "BUY"}], date="2026-07-01", path=p)
            J.log_calls([{"ticker": "NVDA", "action": "BUY"}], date="2026-07-02", path=p)
            self.assertEqual(len(J.load_journal(p)), 1)
            # a flip to TRIM opens a NEW episode → logged
            J.log_calls([{"ticker": "NVDA", "action": "TRIM"}], date="2026-07-03", path=p)
            self.assertEqual(len(J.load_journal(p)), 2)

    def test_review_dedups_repeated_rows(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.jsonl"
            # legacy: same call logged 3 days → must count once in the review
            for dt in ("2026-07-01", "2026-07-02", "2026-07-03"):
                with open(p, "a") as f:
                    f.write(json.dumps({"date": dt, "ticker": "NVDA", "action": "BUY",
                                        "entry": 100}) + "\n")
            r = J.review_outcomes(p)
            self.assertEqual(r["n"], 1)


class TestTranslationGuard(unittest.TestCase):
    def test_numbers_and_tickers_must_survive(self):
        src = "Trim NVDA $300, stop $190. Buy TSM $500."
        self.assertTrue(S._tokens_preserved(src, "减仓 NVDA $300，止损 $190。买入 TSM $500。"))
        self.assertFalse(S._tokens_preserved(src, "减仓 NVDA $30，止损 $190。买入 TSM $500。"))  # 300→30
        self.assertFalse(S._tokens_preserved(src, "减仓 NVDA $300，止损 $190。买入 $500。"))     # TSM dropped


class TestRepeatCounts(unittest.TestCase):
    def test_counts_consecutive_prior_days(self):
        journal = [{"date": "2026-06-30", "ticker": "INTC", "action": "TRIM"},
                   {"date": "2026-06-29", "ticker": "INTC", "action": "TRIM"}]
        calls = [{"ticker": "INTC", "action": "TRIM"}]
        # 2026-07-01 is a Wednesday; 6/30 Tue + 6/29 Mon = 2 consecutive days
        self.assertEqual(J.repeat_counts(calls, journal, today="2026-07-01"), {"INTC": 2})

    def test_no_repeat_when_action_changed(self):
        journal = [{"date": "2026-06-30", "ticker": "INTC", "action": "TRIM"}]
        calls = [{"ticker": "INTC", "action": "SELL"}]         # escalated, not a repeat
        self.assertEqual(J.repeat_counts(calls, journal, today="2026-07-01"), {})

    def test_weekend_gap_does_not_break_streak(self):
        journal = [{"date": "2026-06-26", "ticker": "TSM", "action": "BUY"}]   # Friday
        calls = [{"ticker": "TSM", "action": "BUY"}]
        # today Monday 6/29: Sat/Sun skipped, Friday counts
        self.assertEqual(J.repeat_counts(calls, journal, today="2026-06-29"), {"TSM": 1})

    def test_fresh_call_absent(self):
        self.assertEqual(J.repeat_counts([{"ticker": "LLY", "action": "BUY"}], [],
                                         today="2026-07-01"), {})


class TestDigest(unittest.TestCase):
    def test_digest_under_cap(self):
        ctx = {"date": "2026-06-24",
               "macro": {"label": "NEUTRAL", "exposure": {"net_target": 0.7}},
               "unwind": {"band": "elevated", "score": 50},
               "portfolio": {"actions": ["Trim winners."]},
               "calls": [{"ticker": "MU", "action": "TRIM", "reason": "extended"},
                         {"ticker": "NVDA", "action": "KEEP"}],
               "ideas": {"equity": [{"ticker": "AVGO", "setup": "momentum"}]}}
        d = S.build_digest(ctx)
        self.assertLessEqual(len(d), 1800)
        self.assertIn("TRIM", d)
        self.assertIn("MU", d)
        self.assertIn("KEEP (1): NVDA", d)
        # the dev-y research-skipped note must not leak into the phone message
        self.assertNotIn("deep research", d)

    def test_md_to_text_flattens_tables(self):
        md = ("## Calls\n\n| t | act | $ | reason |\n|---|---|--:|---|\n"
              "| MU | BUY | $328 | strong |\n| QQQ | KEEP | — | hold |\n")
        txt = S.md_to_text(md)
        self.assertNotIn("|", txt)                      # no pipe tables
        self.assertIn("• MU · BUY · $328 · strong", txt)
        self.assertIn("• QQQ · KEEP · hold", txt)       # the '—' cell dropped
        self.assertIn("【Calls】", txt)

    def test_chunk_md_splits_and_preserves(self):
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(120))
        parts = S.chunk_md(text, size=1500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 1560 for p in parts))   # within cap (+ one long line slack)
        # nothing dropped: every source line appears in the reassembled parts
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))

    def test_digest_warning_banner(self):
        ctx = {"date": "2026-06-24", "mode": "preopen",
               "warnings": ["market data fetch returned NOTHING (yfinance down)"],
               "macro": {"label": "NEUTRAL", "exposure": {"net_target": 0.7}},
               "unwind": {"band": "low", "score": 0},
               "portfolio": {}, "calls": [], "ideas": {}}
        d = S.build_digest(ctx)
        self.assertTrue(d.startswith("⚠️ DESK WARNING"))
        self.assertIn("yfinance down", d)

    def test_digest_groups_buy_sell(self):
        ctx = {"date": "2026-06-24", "mode": "wrap",
               "macro": {"label": "RISK_ON_TREND", "exposure": {"net_target": 0.9}},
               "unwind": {"band": "low", "score": 12},
               "portfolio": {"actions": ["Raise cash to 90%."]},
               "calls": [{"ticker": "SNDK", "action": "SELL", "reason": "downgrade",
                          "dollars": 1375, "when": "at the open"},
                         {"ticker": "AMD", "action": "NEW_BUY", "reason": "leader",
                          "dollars": 550, "when": "scale in on a dip toward $140.00"}],
               "ideas": {"equity": []}}
        d = S.build_digest(ctx)
        self.assertIn("🔴 SELL", d)
        self.assertIn("SNDK", d)
        self.assertIn("$1,375", d)              # dollar amount shown
        self.assertIn("at the open", d)         # timing shown
        self.assertIn("🟢 BUY / ADD", d)
        self.assertIn("AMD (new)", d)
        self.assertIn("$550", d)


if __name__ == "__main__":
    unittest.main()
