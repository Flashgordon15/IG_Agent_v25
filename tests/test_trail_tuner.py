"""Tests for MFE/MAE trail tuner and per-epic trail config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))
sys.path.insert(0, str(ROOT / "src"))

from research import trail_tuner as tuner

from trading.trail_config import (
    get_trail_overrides_for_epic,
    reset_trail_config_cache_for_tests,
)


def test_mfe_mae_points_buy() -> None:
    mfe, mae = tuner.mfe_mae_points(
        direction="BUY", entry=100.0, fwd_high=110.0, fwd_low=95.0
    )
    assert mfe == 10.0
    assert mae == 5.0


def test_simulate_trail_r_buy_win() -> None:
    row = {
        "direction": "BUY",
        "entry": 100.0,
        "atr": 10.0,
        "fwd_high_6": 115.0,
        "fwd_low_6": 99.0,
    }
    r = tuner.simulate_trail_r(row, trigger_mult=0.5, distance_mult=1.0, stop_pts=10.0)
    assert r >= 0.5


def test_tune_epic_picks_sweep_values() -> None:
    rows = []
    for i in range(40):
        rows.append(
            {
                "epic": "IX.D.NIKKEI.IFM.IP",
                "fired": True,
                "direction": "BUY" if i % 2 == 0 else "SELL",
                "entry": 1000.0 + i,
                "atr": 20.0,
                "fwd_high_6": 1025.0 + i,
                "fwd_low_6": 995.0 + i,
            }
        )
    result = tuner.tune_epic(epic="IX.D.NIKKEI.IFM.IP", rows=rows)
    assert result["fired_signals"] == 40
    assert result["trail_trigger_atr_multiple"] in tuner._TRIGGER_SWEEP
    assert result["trail_distance_atr_multiple"] in tuner._DISTANCE_SWEEP
    assert result["median_capture_ratio"] >= 0.0


def test_trail_config_reads_tuned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_trail_config_cache_for_tests()
    lake = tmp_path / "data_lake" / "state"
    lake.mkdir(parents=True)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (lake / "trail_epic_overrides.json").write_text(
        json.dumps(
            {
                "by_epic": {
                    "CS.D.CFPGOLD.CFP.IP": {
                        "trail_trigger_atr_multiple": 0.6,
                        "trail_distance_atr_multiple": 1.25,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (cfg / "config_v26.json").write_text(
        json.dumps({"trailing_stop": {"epic_overrides": {}}}),
        encoding="utf-8",
    )

    import trading.trail_config as tc

    monkeypatch.setattr(tc, "_project_root", lambda: tmp_path)
    reset_trail_config_cache_for_tests()
    overrides = get_trail_overrides_for_epic("CS.D.CFPGOLD.CFP.IP")
    assert overrides["trail_trigger_atr_multiple"] == 0.6
    assert overrides["trail_distance_atr_multiple"] == 1.25
    reset_trail_config_cache_for_tests()
