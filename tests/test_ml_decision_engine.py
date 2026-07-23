"""Tests for ML decision engine and setup memory."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from data.ml_training_store import set_store_path_for_tests
from ml.decision_engine import blend_ml_confidence
from ml.setup_memory import evaluate_setup_memory
from system.config import Config


def _cfg(**overrides) -> Config:
    data = {
        "USE_ML_SIGNAL": True,
        "ml_min_rows_for_model": 50,
        "stop_distance_points": 4.0,
        "ml_learning": {
            "setup_memory_enabled": True,
            "setup_memory_min_trades": 3,
            "setup_memory_veto_wr": 0.25,
            "setup_memory_penalty_wr": 0.40,
            "setup_memory_penalty_pts": 10.0,
        },
        "interim_scorer_weights": {
            "trend": 25,
            "session": 25,
            "volatility": 25,
            "recent_performance": 25,
        },
    }
    data.update(overrides)
    return Config(_data=data)


class SetupMemoryTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_store_path_for_tests(None)

    def test_veto_chronic_loser_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ml_training_store.jsonl"
            setup = "SELL|bear|late|atr0-30|rsilow|volhigh"
            lines = []
            for i in range(5):
                lines.append(
                    json.dumps(
                        {
                            "setup_name": setup,
                            "result": "LOSS",
                            "exit_time": f"2026-07-0{i+1}T10:00:00+00:00",
                        }
                    )
                )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            set_store_path_for_tests(path)
            verdict = evaluate_setup_memory(_cfg(), setup)
            self.assertTrue(verdict.veto)
            self.assertGreaterEqual(verdict.trades, 3)


class DecisionEngineTests(unittest.TestCase):
    def test_setup_veto_zeros_confidence(self) -> None:
        with patch("ml.setup_memory.evaluate_setup_memory") as mock_mem:
            with patch("ml.feed_quality.evaluate_feed_quality") as mock_feed:
                with patch("system.ml_filter_overrides.evaluate_filter_block", return_value=(False, "")):
                    from ml.feed_quality import FeedQualityVerdict

                    mock_feed.return_value = FeedQualityVerdict()
                    from ml.setup_memory import SetupMemoryVerdict

                    mock_mem.return_value = SetupMemoryVerdict(
                        setup_key="bad",
                        trades=6,
                        wins=1,
                        win_rate=0.16,
                        penalty_pts=0.0,
                        veto=True,
                        reason="test",
                    )
                    result = blend_ml_confidence(
                        cfg=_cfg(),
                        market="Nikkei",
                        direction="BUY",
                        snapshot={},
                        store=None,
                        rules_conf=72.0,
                        setup_key="bad",
                    )
        self.assertEqual(result.confidence, 0.0)
        self.assertTrue(result.setup_veto)

    def test_interim_blend_when_model_untrained(self) -> None:
        interim = MagicMock()
        interim.total = 80.0
        interim.notes = "test"
        from ml.feed_quality import FeedQualityVerdict
        from ml.profit_policy import ProfitPolicyVerdict
        from ml.setup_memory import SetupMemoryVerdict

        with patch("trading.ml_scorer.get_ml_scorer") as mock_scorer:
            mock_scorer.return_value.is_trained.return_value = False
            with patch("ml.interim_scorer.should_use_interim_scorer", return_value=True):
                with patch("ml.interim_scorer.get_interim_scorer") as mock_get:
                    with patch("ml.feed_quality.evaluate_feed_quality") as mock_feed:
                        with patch(
                            "system.ml_filter_overrides.evaluate_filter_block",
                            return_value=(False, ""),
                        ):
                            with patch("ml.profit_policy.apply_profit_policy") as mock_pol:
                                with patch("ml.setup_memory.evaluate_setup_memory") as mock_mem:
                                    mock_feed.return_value = FeedQualityVerdict()
                                    mock_pol.return_value = ProfitPolicyVerdict(
                                        confidence=75.0
                                    )
                                    mock_get.return_value.score.return_value = interim
                                    mock_mem.return_value = SetupMemoryVerdict(
                                        setup_key="ok",
                                        trades=0,
                                        wins=0,
                                        win_rate=0.0,
                                        penalty_pts=0.0,
                                        veto=False,
                                        reason="ok",
                                    )
                                    result = blend_ml_confidence(
                                        cfg=_cfg(),
                                        market="Nikkei",
                                        direction="BUY",
                                        snapshot={"last": {"rsi": 55, "atr": 10}},
                                        store=None,
                                        rules_conf=70.0,
                                        setup_key="ok",
                                    )
        self.assertEqual(result.mode, "interim")
        self.assertGreater(result.confidence, 0.0)
        self.assertTrue(result.blended)
