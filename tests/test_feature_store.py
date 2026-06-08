"""Tests for v26 feature store builder."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.feature_store import _flatten_fill


def test_flatten_fill_extracts_direction_from_setup_key() -> None:
    row = {
        "ts": "2026-06-08T01:32:22Z",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "market": "Japan 225",
        "payload": {
            "setup_key": "SELL|bear|asia_early|atr180-210|rsilow|volnormal",
            "pnl_gbp": -52.6,
            "result": "LOSS",
        },
    }
    flat = _flatten_fill(row)
    assert flat["direction"] == "SELL"
    assert flat["setup_key"].startswith("SELL|")
