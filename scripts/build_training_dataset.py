#!/usr/bin/env python3
"""
Merge replay_results.jsonl (weight 1.0) and ml_training_store.jsonl (weight 3.0)
into training_dataset.csv.

  PYTHONPATH=src python3 scripts/build_training_dataset.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.engine_log import log_engine
from system.paths import data_dir

REPLAY_PATH = data_dir() / "replay_results.jsonl"
ML_PATH = data_dir() / "ml_training_store.jsonl"
OUT_PATH = data_dir() / "training_dataset.csv"

REPLAY_WEIGHT = 1.0
ML_WEIGHT = 3.0
SHADOW_WEIGHT = 1.0


def _warn(message: str) -> None:
    try:
        log_engine(message)
    except Exception:
        print(message, file=sys.stderr)


def _stable_key(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("timestamp") or row.get("entry_time") or ""),
        str(row.get("deal_id") or ""),
        str(row.get("setup_key") or row.get("setup_name") or ""),
        str(row.get("direction") or ""),
    ]
    key = "|".join(parts)
    if key.replace("|", "").strip():
        return key
    return json.dumps(row, sort_keys=True, default=str)


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        _warn(f"build_training_dataset: missing {label} at {path}")
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _warn(
                    f"build_training_dataset: skip invalid JSON in {path.name} "
                    f"line {line_no}"
                )
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


def _flatten_row(row: dict[str, Any], sample_weight: float) -> dict[str, str]:
    out = {k: _cell_value(v) for k, v in row.items()}
    out["sample_weight"] = _cell_value(sample_weight)
    return out


def _load_shadow_rows() -> list[dict[str, Any]]:
    try:
        from data.learning_store import LearningStore
        from data.shadow_training_registry import list_for_ml_training
        from system.paths import data_dir

        db = data_dir() / "learning_db.sqlite3"
        if not db.is_file():
            _warn("build_training_dataset: no learning_db for shadow registry")
            return []
        store = LearningStore(str(db))
        store.connect()
        return list_for_ml_training(store.conn)
    except Exception as exc:
        _warn(f"build_training_dataset: shadow registry load failed: {exc}")
        return []


def main() -> int:
    replay_raw = _load_jsonl(REPLAY_PATH, label="replay_results.jsonl")
    ml_raw = _load_jsonl(ML_PATH, label="ml_training_store.jsonl")
    shadow_raw = _load_shadow_rows()

    replay_sorted = sorted(replay_raw, key=_stable_key)
    ml_sorted = sorted(ml_raw, key=_stable_key)
    shadow_sorted = sorted(shadow_raw, key=_stable_key)

    replay_rows = [_flatten_row(r, REPLAY_WEIGHT) for r in replay_sorted]
    ml_rows = [_flatten_row(r, ML_WEIGHT) for r in ml_sorted]
    shadow_rows = [_flatten_row(r, SHADOW_WEIGHT) for r in shadow_sorted]
    all_rows = replay_rows + ml_rows + shadow_rows

    all_keys: set[str] = set()
    for row in all_rows:
        all_keys.update(row)
    data_fields = sorted(k for k in all_keys if k != "sample_weight")
    fieldnames = data_fields + (["sample_weight"] if "sample_weight" in all_keys else [])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    replay_n = len(replay_rows)
    ml_n = len(ml_rows)
    shadow_n = len(shadow_rows)
    total = len(all_rows)
    print(
        f"Replay rows: {replay_n} | Training rows: {ml_n} | "
        f"Shadow rows: {shadow_n} | Total: {total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
