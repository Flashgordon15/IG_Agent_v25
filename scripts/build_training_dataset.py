#!/usr/bin/env python3
"""Merge replay + ML training JSONL into training_dataset.csv (weight 1× / 3×)."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.paths import data_dir

REPLAY_PATH = data_dir() / "replay_results.jsonl"
ML_PATH = data_dir() / "ml_training_store.jsonl"
OUT_PATH = data_dir() / "training_dataset.csv"

FEATURE_KEYS = (
    "confidence",
    "rsi",
    "atr",
    "spread",
    "fitness_score",
    "session_window",
    "volume_regime",
    "trend_bias",
)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _row_features(row: dict) -> dict:
    out: dict[str, float | str] = {}
    for k in FEATURE_KEYS:
        v = row.get(k)
        if v is None:
            out[k] = 0.0 if k != "session_window" else "unknown"
        elif isinstance(v, (int, float)):
            out[k] = float(v)
        else:
            out[k] = str(v)
    label = row.get("label") or row.get("result") or row.get("fwd_6_win")
    if label in (True, 1, "1", "WIN", "win"):
        out["label"] = 1
    elif label in (False, 0, "0", "LOSS", "loss"):
        out["label"] = 0
    else:
        try:
            out["label"] = int(bool(label))
        except (TypeError, ValueError):
            out["label"] = 0
    return out


def main() -> int:
    merged: list[dict] = []
    for row in _load_jsonl(REPLAY_PATH):
        merged.append(_row_features(row))
    for row in _load_jsonl(ML_PATH):
        feat = _row_features(row)
        merged.extend([feat] * 3)

    if not merged:
        print("No rows — create replay_results.jsonl or ml_training_store.jsonl first")
        return 1

    fieldnames = list(FEATURE_KEYS) + ["label"]
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in merged:
            w.writerow({k: row.get(k, 0) for k in fieldnames})

    labels = Counter(int(r.get("label", 0)) for r in merged)
    print(f"{len(merged)} rows, {len(FEATURE_KEYS)} features, label distribution: {dict(labels)}")
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
