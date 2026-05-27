"""Tests for data.ml_training_store."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import data.ml_training_store as mls
from data.ml_training_store import MLTrainingStore


def _entry() -> dict:
    return {
        "confidence": 86.0,
        "confidence_band": "standard",
        "setup_name": "BUY|bull|asia_early|atr30-60|rsimid|volnormal",
        "trend_bias": "bull",
        "rsi": 62.0,
        "atr": 20.0,
        "spread": 7.0,
        "volume_regime": "volnormal",
        "session_window": "asia_early",
        "entry_price": 100.0,
        "entry_time": "2026-05-27T01:00:00+00:00",
        "fitness_score": 72.0,
        "points_state": "HEALTHY",
        "size_multiplier": 0.5,
        "instrument": "Japan 225",
        "source": "agent",
    }


def _exit(*, confirmed: bool, ig_pnl: float | None = None) -> dict:
    data = {
        "exit_price": 110.0,
        "exit_time": "2026-05-27T02:00:00+00:00",
        "pts_pnl": 10.0,
        "gbp_pnl": 42.0,
        "exit_reason": "trail",
        "result": "WIN",
        "points_scored": 2.5,
        "confirmed": confirmed,
        "source": "agent",
    }
    if ig_pnl is not None:
        data["ig_pnl_currency"] = ig_pnl
    return data


class MLTrainingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ml_training_store.jsonl"
        mls.reset_ml_training_store_for_tests()
        mls.set_store_path_for_tests(self.path)
        self.store = MLTrainingStore(self.path)

    def tearDown(self) -> None:
        mls.reset_ml_training_store_for_tests()
        self.tmp.cleanup()

    def test_entry_buffer_and_pending(self) -> None:
        self.store.record_entry("DEAL1", _entry())
        self.assertTrue(self.store.is_pending("DEAL1"))
        self.assertFalse(self.store.is_pending("OTHER"))

    def test_confirmed_false_skips_write(self) -> None:
        self.store.record_entry("DEAL1", _entry())
        self.store.record_exit("DEAL1", _exit(confirmed=False))
        self.assertEqual(self.store.record_count(), 0)
        self.assertTrue(self.store.is_pending("DEAL1"))

    def test_confirmed_true_writes_record(self) -> None:
        self.store.record_entry("DEAL1", _entry())
        self.store.record_exit("DEAL1", _exit(confirmed=True, ig_pnl=42.0))
        self.assertEqual(self.store.record_count(), 1)
        self.assertFalse(self.store.is_pending("DEAL1"))
        line = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(line["deal_id"], "DEAL1")
        self.assertTrue(line["confirmed"])
        self.assertAlmostEqual(line["gbp_pnl"], 42.0)
        self.assertEqual(line["version"], "25.1.0")
        self.assertEqual(set(line.keys()), set(mls.REQUIRED_FIELDS))

    def test_sim_exclusion(self) -> None:
        entry = _entry()
        entry["source"] = "sim"
        self.store.record_entry("DEAL1", entry)
        self.assertFalse(self.store.is_pending("DEAL1"))
        self.store.record_exit("DEAL1", _exit(confirmed=True, ig_pnl=1.0))
        self.assertEqual(self.store.record_count(), 0)

    def test_exit_without_entry_skips(self) -> None:
        self.store.record_exit("MISSING", _exit(confirmed=True, ig_pnl=1.0))
        self.assertEqual(self.store.record_count(), 0)

    def test_append_not_overwrite(self) -> None:
        self.store.record_entry("A", _entry())
        self.store.record_exit("A", _exit(confirmed=True, ig_pnl=1.0))
        self.store.record_entry("B", _entry())
        self.store.record_exit("B", _exit(confirmed=True, ig_pnl=2.0))
        self.assertEqual(self.store.record_count(), 2)
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["deal_id"], "A")
        self.assertEqual(json.loads(lines[1])["deal_id"], "B")

    def test_fsync_called_on_write(self) -> None:
        self.store.record_entry("DEAL1", _entry())
        with patch("data.ml_training_store.os.fsync") as fsync_mock:
            self.store.record_exit("DEAL1", _exit(confirmed=True, ig_pnl=10.0))
        fsync_mock.assert_called()

    def test_never_raises_on_write_error(self) -> None:
        self.store.record_entry("DEAL1", _entry())
        with patch("data.ml_training_store._append_line", side_effect=OSError("disk")):
            self.store.record_exit("DEAL1", _exit(confirmed=True, ig_pnl=1.0))
        self.assertEqual(self.store.record_count(), 0)

    def test_confirmed_from_ig_row_helper(self) -> None:
        self.assertFalse(MLTrainingStore.confirmed_from_ig_row({}))
        self.assertTrue(
            MLTrainingStore.confirmed_from_ig_row({"ig_pnl_currency": 12.5})
        )


if __name__ == "__main__":
    unittest.main()
