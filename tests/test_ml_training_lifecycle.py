"""ML training hooks fire on mock entry + exit lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data.ml_training_store import (
    MLTrainingStore,
    reset_ml_training_store_for_tests,
    set_store_path_for_tests,
)
from data.models import Quote
from datetime import datetime
from execution.ml_training_hooks import (
    configure_ml_training,
    record_ml_entry_from_signal,
    record_ml_exit_for_deal,
)
from execution.types import TradeSignal


class MLTrainingLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ml.jsonl"
        set_store_path_for_tests(self.path)
        self.store = MLTrainingStore(self.path)
        configure_ml_training(ml_store=self.store)

    def tearDown(self) -> None:
        configure_ml_training(ml_store=None)
        reset_ml_training_store_for_tests()
        self.tmp.cleanup()

    def test_entry_and_exit_hooks(self) -> None:
        q = Quote(time=datetime.now(), bid=100.0, offer=100.5)
        sig = TradeSignal(
            market="Japan 225",
            epic="IX.D.NIKKEI.IFM.IP",
            direction="BUY",
            raw_confidence=85.0,
            adjusted_confidence=88.0,
            setup_key="TEST|buy",
            quote=q,
            snapshot={"last": {"rsi": 55.0, "atr": 25.0}, "vol_regime": "normal"},
            notes="test",
        )
        record_ml_entry_from_signal("DEAL-LIFE", sig, {"size": 1.0}, fill_price=100.25)
        record_ml_exit_for_deal(
            "DEAL-LIFE",
            ig_pnl=-12.5,
            result="LOSS",
            exit_price=99.0,
            pts_pnl=-5.0,
        )
        self.assertTrue(self.path.is_file())
        text = self.path.read_text(encoding="utf-8").strip()
        self.assertIn("DEAL-LIFE", text)
        self.assertIn('"confirmed":true', text.replace(" ", ""))
        self.assertFalse(self.store.is_pending("DEAL-LIFE"))


if __name__ == "__main__":
    unittest.main()
