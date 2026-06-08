"""Tests for S4 offline retrain pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research import s4_retrain as s4


def test_build_s4_rows_from_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        s4,
        "load_replay_rows",
        lambda: [
            {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "fired": True,
                "label_3bar": "WIN",
                "adjusted_score": 80,
                "rsi": 45,
                "atr": 100,
                "atr_ratio": 2.0,
                "timestamp": "2026-01-01T10:00:00",
            },
            {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "fired": True,
                "label_3bar": "LOSS",
                "adjusted_score": 78,
                "rsi": 40,
                "atr": 90,
                "atr_ratio": 1.8,
                "timestamp": "2026-01-01T11:00:00",
            },
        ],
    )
    monkeypatch.setattr(s4, "load_ml_training_records", lambda: [])
    rows = s4.build_s4_rows()
    assert len(rows) == 2
    assert rows[0]["y"] == 1


def test_run_s4_retrain_skips_small_epics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        s4,
        "build_s4_rows",
        lambda: [
            {
                "epic": "X",
                "y": 1,
                "adjusted_score": 80,
                "rsi": 50,
                "atr_ratio": 1.0,
                "timestamp": "t1",
            }
        ],
    )
    monkeypatch.setattr(
        s4,
        "s4_settings",
        lambda: {
            "enabled": False,
            "min_decided_rows": 30,
            "min_val_wr": 0.52,
            "val_holdout_pct": 0.2,
            "models_root": "data_lake/models/s4",
        },
    )
    monkeypatch.setattr(s4, "_project_root", lambda: tmp_path)
    manifest = s4.run_s4_retrain(version="test")
    assert manifest["epics_trained"] == 0
    assert manifest["by_epic"]["X"]["ok"] is False
