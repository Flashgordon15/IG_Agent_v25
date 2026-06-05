"""Tests for scripts/build_training_dataset.py."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_training_dataset.py"
SPEC = importlib.util.spec_from_file_location("build_training_dataset", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules["build_training_dataset"] = builder
SPEC.loader.exec_module(builder)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class BuildTrainingDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.replay_path = base / "replay_results.jsonl"
        self.ml_path = base / "ml_training_store.jsonl"
        self.out_path = base / "training_dataset.csv"
        builder.REPLAY_PATH = self.replay_path
        builder.ML_PATH = self.ml_path
        builder.OUT_PATH = self.out_path

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_row_counts_and_weights(self) -> None:
        replay_rows = [
            {"timestamp": f"2026-05-27T10:0{i}:00+00:00", "deal_id": f"R{i}", "direction": "BUY"}
            for i in range(10)
        ]
        ml_rows = [
            {
                "entry_time": f"2026-05-27T11:0{i}:00+00:00",
                "deal_id": f"M{i}",
                "setup_name": "BUY|bull",
                "direction": "BUY",
            }
            for i in range(5)
        ]
        _write_jsonl(self.replay_path, replay_rows)
        _write_jsonl(self.ml_path, ml_rows)

        self.assertEqual(builder.main(), 0)
        with self.out_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 15)
        replay_weights = {r["deal_id"]: float(r["sample_weight"]) for r in rows if r["deal_id"].startswith("R")}
        ml_weights = {r["deal_id"]: float(r["sample_weight"]) for r in rows if r["deal_id"].startswith("M")}
        self.assertEqual(len(replay_weights), 10)
        self.assertEqual(len(ml_weights), 5)
        self.assertTrue(all(w == 1.0 for w in replay_weights.values()))
        self.assertTrue(all(w == 3.0 for w in ml_weights.values()))

    def test_missing_replay_file(self) -> None:
        ml_rows = [{"entry_time": "2026-05-27T12:00:00+00:00", "deal_id": "M0", "direction": "SELL"}]
        _write_jsonl(self.ml_path, ml_rows)
        self.assertEqual(builder.main(), 0)
        with self.out_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["sample_weight"]), 3.0)

    def test_idempotent_output(self) -> None:
        _write_jsonl(
            self.replay_path,
            [{"timestamp": "2026-05-27T10:00:00+00:00", "deal_id": "R0", "direction": "BUY"}],
        )
        _write_jsonl(
            self.ml_path,
            [{"entry_time": "2026-05-27T11:00:00+00:00", "deal_id": "M0", "direction": "SELL"}],
        )
        builder.main()
        first = self.out_path.read_text(encoding="utf-8")
        builder.main()
        second = self.out_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_missing_ml_file(self) -> None:
        _write_jsonl(
            self.replay_path,
            [{"timestamp": "2026-05-27T10:00:00+00:00", "deal_id": "R0", "direction": "BUY"}],
        )
        self.assertEqual(builder.main(), 0)
        with self.out_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["sample_weight"]), 1.0)


if __name__ == "__main__":
    unittest.main()
