"""Tests for 6-bar label preference."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.label_utils import outcome_label


def test_prefers_6bar_over_3bar() -> None:
    row = {"label_6bar": "WIN", "label_3bar": "LOSS"}
    assert outcome_label(row) == "WIN"


def test_falls_back_to_3bar() -> None:
    row = {"label_3bar": "LOSS"}
    assert outcome_label(row) == "LOSS"
