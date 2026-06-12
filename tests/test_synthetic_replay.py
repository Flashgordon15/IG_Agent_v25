"""Tests for synthetic replay filter override generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data.ml_training_store import set_store_path_for_tests
from system.synthetic_replay import (
    BASELINE_FILTER_OVERRIDES,
    FeatureSnapshot,
    ML_MIN_TRAINING_RECORDS,
    run_synthetic_replay,
)
from system.ml_filter_overrides import ML_RAMP_MIN_RECORDS


class SyntheticReplayThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store_path = self.root / "ml_training_store.jsonl"
        self.meta_path = self.root / "ml_model" / "meta.json"
        set_store_path_for_tests(self.store_path)

    def tearDown(self) -> None:
        set_store_path_for_tests(None)
        self.tmp.cleanup()

    def _losses(self) -> tuple[list[FeatureSnapshot], list[FeatureSnapshot]]:
        loss = FeatureSnapshot(
            adjusted_score=55.0,
            raw_score=50.0,
            rsi=16.5,
            atr_ratio=0.8,
            result="LOSS",
        )
        win = FeatureSnapshot(
            adjusted_score=80.0,
            raw_score=75.0,
            rsi=55.0,
            atr_ratio=1.0,
            result="WIN",
        )
        return [loss], [loss, win]

    def test_writes_strict_bounds_when_under_training_threshold(self) -> None:
        self.store_path.write_text("{}\n", encoding="utf-8")
        with patch(
            "system.synthetic_replay.load_loss_snapshots",
            return_value=self._losses(),
        ):
            code = run_synthetic_replay(
                store_path=self.store_path,
                meta_path=self.meta_path,
                cycles=1,
            )
        self.assertEqual(code, 0)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        overrides = meta["filter_overrides"]
        self.assertNotEqual(overrides, BASELINE_FILTER_OVERRIDES)
        self.assertTrue(overrides)
        self.assertIn("max_rsi", overrides)
        self.assertLess(overrides["max_rsi"], BASELINE_FILTER_OVERRIDES["max_rsi"])
        replay_meta = meta["synthetic_replay"]
        self.assertEqual(replay_meta["progressive_mode"], "baseline")
        self.assertLess(replay_meta["training_record_count"], ML_RAMP_MIN_RECORDS)
        self.assertEqual(replay_meta["strict_filter_overrides"], overrides)

    def test_replay_bounds_when_at_training_threshold(self) -> None:
        lines = ["{}\n"] * ML_MIN_TRAINING_RECORDS
        self.store_path.write_text("".join(lines), encoding="utf-8")
        with patch(
            "system.synthetic_replay.load_loss_snapshots",
            return_value=self._losses(),
        ):
            code = run_synthetic_replay(
                store_path=self.store_path,
                meta_path=self.meta_path,
                cycles=50,
            )
        self.assertEqual(code, 0)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        overrides = meta["filter_overrides"]
        self.assertNotEqual(overrides, BASELINE_FILTER_OVERRIDES)
        replay_meta = meta["synthetic_replay"]
        self.assertEqual(replay_meta["progressive_mode"], "strict")
        self.assertGreaterEqual(
            replay_meta["training_record_count"],
            ML_MIN_TRAINING_RECORDS,
        )


if __name__ == "__main__":
    unittest.main()
