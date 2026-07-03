"""Parameter tuner — regime-aware auto-tuning tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime import parameter_tuner as pt


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    overlay = tmp_path / "tuning_overlay.json"
    monkeypatch.setattr(pt, "_overlay_path", lambda: overlay)
    monkeypatch.setenv("IG_TUNING_OVERLAY", str(overlay))
    pt.reset_parameter_tuner_for_tests()
    yield
    pt.reset_parameter_tuner_for_tests()


def _synthetic_trades_losing_hv() -> list[dict]:
    rows = []
    for i in range(8):
        rows.append(
            {
                "ticket": f"HV-{i}",
                "epic": "IX.D.DOW.IFM.IP",
                "net_pnl": -5.0 if i < 6 else 12.0,
            }
        )
    return rows


def _synthetic_trades_winning_mr() -> list[dict]:
    rows = []
    for i in range(10):
        rows.append(
            {
                "ticket": f"MR-{i}",
                "epic": "CS.D.CFPGOLD.CFP.IP",
                "net_pnl": 8.0 if i < 8 else -4.0,
            }
        )
    return rows


def test_aggregate_metrics_win_rate():
    trades = _synthetic_trades_winning_mr()
    rmap = {f"MR-{i}": 0 for i in range(10)}
    metrics = pt.aggregate_metrics_by_regime(trades, regime_map=rmap)
    assert metrics[0].trades == 10
    assert metrics[0].win_rate == 0.8
    assert metrics[0].profit_factor > 1.0


def test_tighten_on_failing_hv_regime():
    trades = _synthetic_trades_losing_hv()
    rmap = {f"HV-{i}": 1 for i in range(8)}
    metrics = pt.aggregate_metrics_by_regime(trades, regime_map=rmap)
    assert metrics[1].win_rate < pt.WIN_RATE_TARGET
    current = pt.get_regime_matrix()
    new_matrix, deltas, reasons = pt.compute_regime_adjustments(
        metrics, daily_pnl_gbp=-18.0, current_matrix=current
    )
    assert new_matrix["1"]["size_factor"] < current["1"]["size_factor"]
    assert new_matrix["1"]["trailing_sensitivity"] >= current["1"]["trailing_sensitivity"]
    assert any("hv_trend" in r for r in reasons)


def test_bounds_enforced():
    matrix = {
        "0": {
            "size_factor": 2.0,
            "stop_factor": 2.0,
            "limit_factor": 2.0,
            "trailing_sensitivity": 2.0,
        }
    }
    clamped = pt._clamp_matrix(matrix)
    assert clamped["0"]["size_factor"] <= 1.25
    assert clamped["0"]["stop_factor"] <= 1.50


def test_forbidden_safety_keys():
    errors = pt.validate_tuner_safety({"l1_drawdown_pct": 5.0, "max_daily_loss_gbp": 999})
    assert any("forbidden" in e for e in errors)


def test_run_cycle_persists_overlay(tmp_path, monkeypatch):
    overlay = Path(tmp_path) / "tuning_overlay.json"
    trades = _synthetic_trades_losing_hv()
    rmap = {f"HV-{i}": 1 for i in range(8)}

    monkeypatch.setattr(pt, "harvest_closed_trades", lambda **kw: trades)
    monkeypatch.setattr(pt, "_load_regime_map", lambda: rmap)
    monkeypatch.setattr(pt, "_slippage_by_epic", lambda **kw: {})
    result = pt.run_tuning_cycle(force=True)
    assert result["ok"] is True
    assert overlay.is_file()
    data = json.loads(overlay.read_text())
    assert "regime_matrix" in data
    assert "tuner_history" in data


def test_merge_tuned_gate():
    gate = {"size_factor": 0.85, "stop_factor": 0.9, "limit_factor": 0.85}
    pt._write_overlay_patch(
        regime_matrix={
            "0": {
                "size_factor": 0.75,
                "stop_factor": 0.88,
                "limit_factor": 0.80,
                "trailing_sensitivity": 1.05,
            }
        }
    )
    pt.merge_tuned_gate(gate, 0)
    assert gate["size_factor"] == 0.75
    assert gate["stop_factor"] == 0.88


def test_get_tuner_state_snapshot():
    snap = pt.get_tuner_state_snapshot()
    assert snap["win_rate_target"] == 0.70
    assert snap["daily_pnl_target_gbp"] == 1000.0
    assert "regime_matrix" in snap
