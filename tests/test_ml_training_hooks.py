"""Tests for ML training store live hooks."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from data.ml_training_store import (
    MLTrainingStore,
    reset_ml_training_store_for_tests,
    set_store_path_for_tests,
)
from data.models import Quote
from execution.ml_training_hooks import (
    configure_ml_training,
    record_ml_entry_from_signal,
    record_ml_exit_for_deal,
)
from execution.types import TradeSignal


def _signal() -> TradeSignal:
    q = Quote(time=datetime.now(), bid=100.0, offer=100.5)
    return TradeSignal(
        market="Japan 225",
        epic="IX.D.NIKKEI.IFM.IP",
        direction="BUY",
        raw_confidence=85.0,
        adjusted_confidence=88.0,
        setup_key="BUY|bull",
        quote=q,
        snapshot={"last": {"rsi": 60.0, "atr": 25.0}, "vol_regime": "normal"},
    )


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
        configure_ml_training(ml_store=None)
        reset_ml_training_store_for_tests()
        self.tmp.cleanup()

    def test_record_entry_buffers_deal(self) -> None:
        record_ml_entry_from_signal("DEAL1", _signal(), {"size": 1.0})
        self.assertTrue(self.store.is_pending("DEAL1"))

    def test_entry_exit_append_to_store(self) -> None:
        record_ml_entry_from_signal("DEAL2", _signal(), {"size": 1.0}, fill_price=100.25)
        record_ml_exit_for_deal(
            "DEAL2",
            ig_pnl=15.0,
            result="WIN",
            exit_price=101.0,
            pts_pnl=8.0,
        )
        self.assertTrue(self.path.is_file())
        line = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(line["deal_id"], "DEAL2")
        self.assertTrue(line["confirmed"])
        self.assertFalse(self.store.is_pending("DEAL2"))

    @patch("data.ml_training_store.open", side_effect=IOError("disk full"))
    def test_silent_on_ioerror(self, _mock_open: MagicMock) -> None:
        record_ml_entry_from_signal("DEAL3", _signal(), {"size": 1.0})
        try:
            record_ml_exit_for_deal("DEAL3", ig_pnl=1.0, result="WIN")
        except IOError:
            self.fail("record_ml_exit_for_deal must not propagate IOError")
        self.assertEqual(self.store.record_count(), 0)

    def test_unconfigured_store_is_noop(self) -> None:
        configure_ml_training(ml_store=None)
        try:
            record_ml_entry_from_signal("DEAL4", _signal(), {"size": 1.0})
            record_ml_exit_for_deal("DEAL4", ig_pnl=1.0, result="WIN")
        except Exception as exc:
            self.fail(f"hooks must not raise when store unset: {exc}")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
