"""Tests for ML training store live hooks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from data.ml_training_store import MLTrainingStore, reset_ml_training_store_for_tests, set_store_path_for_tests
from data.models import Quote
from datetime import datetime
from execution.ml_training_hooks import configure_ml_training, record_ml_entry_from_signal
from execution.types import TradeSignal


class MLTrainingHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ml.jsonl"
        set_store_path_for_tests(self.path)
        self.store = MLTrainingStore(self.path)
        self.points = MagicMock()
        self.points.confidence_band.return_value = "standard"
        self.points.get_state.return_value = "CAUTION"
        self.points.get_size_multiplier.return_value = 0.25
        configure_ml_training(ml_store=self.store, points_engine=self.points)

    def tearDown(self) -> None:
        reset_ml_training_store_for_tests()
        self.tmp.cleanup()

    def test_record_entry_buffers_deal(self) -> None:
        q = Quote(time=datetime.now(), bid=100.0, offer=101.0)
        sig = TradeSignal(
            market="japan_225",
            epic="IX.D.NIKKEI.IFM.IP",
            direction="BUY",
            raw_confidence=90.0,
            adjusted_confidence=88.0,
            setup_key="BUY|bull",
            quote=q,
            snapshot={"last": {"rsi": 60.0, "atr": 25.0}, "vol_regime": "normal"},
        )
        record_ml_entry_from_signal("DEAL1", sig, {"size": 1.0})
        self.assertTrue(self.store.is_pending("DEAL1"))


if __name__ == "__main__":
    unittest.main()
