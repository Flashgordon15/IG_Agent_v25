"""Regression tests for profitability assessment improvements (Jun 2026)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyse_replay import _row_label_3, _stats_for_rows  # noqa: E402

from execution.correlation_guard import MAX_NEW_PER_DIRECTION  # noqa: E402
from trading.points_engine import HEALTHY_CUMULATIVE_MIN, PointsEngine  # noqa: E402


class TestReplayLabelFix(unittest.TestCase):
    def test_label_3bar_alias(self):
        self.assertEqual(_row_label_3({"label_3bar": "WIN"}), "WIN")

    def test_label_3_batch_replay(self):
        rows = [{"label_3": "WIN"}, {"label_3": "LOSS"}, {"label_3": "WIN"}]
        n, wr, pf = _stats_for_rows(rows)
        self.assertEqual(n, 3)
        self.assertAlmostEqual(wr, 100.0 * 2 / 3, places=1)
        self.assertGreater(pf, 1.0)


class TestCorrelationCap(unittest.TestCase):
    def test_max_per_direction_lowered(self):
        self.assertEqual(MAX_NEW_PER_DIRECTION, 5)


class TestHealthyThreshold(unittest.TestCase):
    def test_healthy_at_four(self):
        self.assertEqual(HEALTHY_CUMULATIVE_MIN, 4.0)
        engine = PointsEngine(state_path=Path(tempfile.mkdtemp()) / "pts.json")
        engine._cumulative = 4.5
        self.assertEqual(engine.get_state(), "HEALTHY")


class TestPartialCloseConfigGuard(unittest.TestCase):
    def test_skipped_when_disabled(self):
        from trading.trade_manager import TradeManager

        store = MagicMock()
        store.is_partial_close_done.return_value = False
        cfg = MagicMock()
        cfg.partial_close_enabled = False

        tm = TradeManager(store, cfg, rest_client=None, points_engine=None)
        out = tm._apply_partial_close(
            "m", "BUY", 1, 100.0, 1.0, 110.0, 10.0, 90.0, "", "EPIC"
        )
        self.assertEqual(out, [])


class TestMlMinRecordsGate(unittest.TestCase):
    def test_blend_skipped_below_500_records(self):
        from trading.trading_loop import TradingLoop

        loop = object.__new__(TradingLoop)
        loop._config = MagicMock()
        loop._config.get = lambda k, d=False: k == "USE_ML_SIGNAL"
        loop._config.signal_threshold = 80
        loop._config.stop_distance_points = 45
        loop._market = "Japan 225"
        loop._ml_decision_log = []
        loop._signal_engine = MagicMock()
        loop._points = MagicMock()
        loop._points.trade_confidence_threshold.return_value = 80
        loop._points.get_threshold.return_value = 80
        loop._points.min_size_confidence_threshold.return_value = 80
        loop._points.get_state.return_value = "HEALTHY"

        sig = MagicMock()
        sig.signal = "BUY"
        sig.adjusted_confidence = 88.0
        sig.setup_key = "test"
        sig.snapshot = {"last": {"atr": 30, "rsi": 60}, "raw_confidence": 88}
        loop._signal_engine.evaluate.return_value = sig

        scorer = MagicMock()
        scorer.is_trained.return_value = True
        scorer.feature_names = ["adjusted_score", "raw_score", "rsi", "atr_ratio"]
        scorer.score.return_value = 0.7

        empty_meta_dir = Path(tempfile.mkdtemp())

        with (
            patch("trading.ml_scorer.get_ml_scorer", return_value=scorer),
            patch("data.ml_training_store.MLTrainingStore") as mock_store_cls,
            patch("system.paths.data_dir", return_value=empty_meta_dir),
            patch("trading.trading_loop.log_engine") as mock_log,
        ):
            mock_store_cls.return_value.record_count.return_value = 11
            gate = loop._gate_signal_confidence()

        self.assertTrue(gate.passed)
        mock_log.assert_any_call("ML blend skipped: 11 training records (need 500)")


if __name__ == "__main__":
    unittest.main()
