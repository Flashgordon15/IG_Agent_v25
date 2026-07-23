"""Tests for profit policy and feed quality gates."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ml.profit_policy import apply_profit_policy
from system.config import Config


def _cfg(**overrides) -> Config:
    data = {
        "profit_philosophy": {
            "enabled": True,
            "marginal_ml_veto": True,
            "min_ml_probability": 0.52,
            "session_cold_wr": 0.45,
            "session_cold_penalty_pts": 8.0,
            "min_recent_for_session_adj": 3,
        }
    }
    data.update(overrides)
    return Config(_data=data)


class ProfitPolicyTests(unittest.TestCase):
    def test_marginal_ml_veto(self) -> None:
        result = apply_profit_policy(_cfg(), 72.0, ml_prob=0.48, store=None)
        self.assertTrue(result.veto)
        self.assertEqual(result.confidence, 0.0)

    def test_session_cold_penalty(self) -> None:
        store = MagicMock()
        store.recent_confirmed_closed_trades.return_value = [
            {"result": "LOSS"},
            {"result": "LOSS"},
            {"result": "LOSS"},
        ]
        result = apply_profit_policy(_cfg(), 70.0, ml_prob=0.6, store=store)
        self.assertFalse(result.veto)
        self.assertEqual(result.confidence, 62.0)

    def test_passes_good_ml(self) -> None:
        result = apply_profit_policy(_cfg(), 70.0, ml_prob=0.62, store=None)
        self.assertFalse(result.veto)
        self.assertEqual(result.confidence, 70.0)
