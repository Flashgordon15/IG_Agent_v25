"""Tests for tuning_params overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analytics import tuning_params as tp


@pytest.fixture(autouse=True)
def _isolate_overlay(tmp_path, monkeypatch):
    overlay = tmp_path / "tuning_overlay.json"
    monkeypatch.setattr(tp, "_OVERLAY_PATH", overlay)
    monkeypatch.setenv("IG_TUNING_OVERLAY", str(overlay))
    tp.reset_tuning_overlay_for_tests()
    yield
    tp.reset_tuning_overlay_for_tests()


def test_get_tuning_params_defaults():
    out = tp.get_tuning_params()
    assert out["ok"] is True
    assert out["params"]["z_score_entry_min"] == -2.0
    assert "rotation_weight_volatility" in out["params"]


def test_apply_valid_update():
    result = tp.apply_tuning_update({"vol_filter_min_tpm": 8.0})
    assert result["ok"] is True
    assert result["applied"]["vol_filter_min_tpm"] == 8.0
    merged = tp.get_tuning_params()
    assert merged["params"]["vol_filter_min_tpm"] == 8.0


def test_reject_forbidden_iron_cage_key():
    result = tp.apply_tuning_update({"max_daily_loss_gbp": 9999})
    assert result["ok"] is False
    assert any("forbidden" in e for e in result["errors"])


def test_reject_out_of_bounds():
    result = tp.apply_tuning_update({"z_score_entry_max": 99.0})
    assert result["ok"] is False
    assert any("out_of_bounds" in e for e in result["errors"])


def test_persist_overlay_file(tmp_path):
    path = Path(tmp_path) / "tuning_overlay.json"
    tp.apply_tuning_update({"trailing_sensitivity": 1.5})
    data = json.loads(path.read_text())
    assert data["params"]["trailing_sensitivity"] == 1.5
