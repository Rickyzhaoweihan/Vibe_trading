#!/usr/bin/env python3
"""Unit tests for deepresearch stop/target parsing.

A WRONG stop-loss is worse than a blank one (the user may act on it), so the
parsers must reject loose prose and only return a level adjacent to a '$'.
Pure string parsing — no network, no LLM."""

import sys
import unittest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(BOT.parent / "TradingAgents"))
import deepresearch as DR


class TestParseStop(unittest.TestCase):
    def test_stop_loss_phrasing(self):
        self.assertEqual(DR._parse_stop("set a stop loss at $79 below entry"), 79.0)
        self.assertEqual(DR._parse_stop("a stop-loss of $1003 protects the core"), 1003.0)

    def test_trailing_dollar_stop(self):
        # the form seen in real synthesis prose: "$1,023 stop"
        self.assertEqual(DR._parse_stop("relying on the $1,023 stop as a hard floor"), 1023.0)

    def test_comma_thousands(self):
        self.assertEqual(DR._parse_stop("stop near $2,005 on a break"), 2005.0)

    def test_rejects_loose_prose(self):
        # 'stop' near a non-dollar number must NOT yield a bogus stop
        self.assertIsNone(DR._parse_stop("we stop and note ATR is 2.17% today"))
        self.assertIsNone(DR._parse_stop("hold for 1-2 quarters, do not stop chasing"))

    def test_none_when_absent(self):
        self.assertIsNone(DR._parse_stop("a balanced hold with no level given"))


class TestParseTarget(unittest.TestCase):
    def test_target_phrasings(self):
        self.assertEqual(DR._parse_target("a price target of $250 by Q3"), 250.0)
        self.assertEqual(DR._parse_target("upside target $312 on a breakout"), 312.0)

    def test_rejects_loose_prose(self):
        self.assertIsNone(DR._parse_target("they target growth of 15% next year"))


if __name__ == "__main__":
    unittest.main()
