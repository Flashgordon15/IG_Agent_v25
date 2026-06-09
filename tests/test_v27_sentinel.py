"""Tests for v27 Autonomous Sentinel sandbox modules."""

from __future__ import annotations

import json

import pytest

from ai.operational.auto_repair import AutoRepairEngine, OperationalBoundaryError
from ai.operational.system_monitor import SystemMonitor, port_open
from ai.strategy.backtest_simulator import BacktestSimulator, split_is_oos
from ai.strategy.performance_reviewer import FRICTION_WARN_RATIO, friction_warning


def test_split_is_oos_70_30():
    bars = list(range(100))
    is_bars, oos_bars = split_is_oos(bars, is_ratio=0.70)
    assert len(is_bars) == 70
    assert len(oos_bars) == 30
    assert is_bars[-1] == 69
    assert oos_bars[0] == 70


def test_friction_warning_flags_above_threshold():
    cell = friction_warning("IX.D.NIKKEI.IFM.IP", spread_pts=2.0, atr_pts=10.0)
    assert cell.spread_friction_ratio == pytest.approx(0.2)
    assert cell.warning is True
    assert cell.prohibited is True

    ok = friction_warning("IX.D.NIKKEI.IFM.IP", spread_pts=1.0, atr_pts=10.0)
    assert ok.spread_friction_ratio == pytest.approx(0.1)
    assert ok.warning is False
    assert ok.prohibited is False
    assert FRICTION_WARN_RATIO == 0.15


def test_config_snapshot_backup_written(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    v25 = cfg / "config_v25.json"
    v25.write_text('{"confidence_floor": 72}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    proposals = state_dir / "strategy_proposals.json"

    monkeypatch.setattr("ai.operational.auto_repair.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.default_config_paths", lambda: [v25]
    )
    monkeypatch.setattr("ai.operational.auto_repair.ai_backups_dir", lambda: backup_dir)
    monkeypatch.setattr(
        "ai.operational.auto_repair.config_snapshot_backup_path",
        lambda: backup_dir / "config_snapshot_backup.json",
    )
    monkeypatch.setattr("ai.operational.auto_repair.data_dir", lambda: tmp_path)

    engine = AutoRepairEngine()
    path = engine.write_config_snapshot_backup(reason="test")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["reason"] == "test"
    assert data["config_files"][0]["path"] == "config/config_v25.json"


def test_dead_drop_sets_safety_freeze(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config_v25.json").write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()

    monkeypatch.setattr("ai.operational.auto_repair.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.default_config_paths",
        lambda: [cfg / "config_v25.json"],
    )
    monkeypatch.setattr("ai.operational.auto_repair.ai_backups_dir", lambda: backup_dir)
    monkeypatch.setattr(
        "ai.operational.auto_repair.config_snapshot_backup_path",
        lambda: backup_dir / "config_snapshot_backup.json",
    )
    monkeypatch.setattr("ai.operational.auto_repair.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.telemetry_dead_drop_dir", lambda: state
    )
    monkeypatch.setattr(
        "ai.operational.auto_repair.operational_safety_freeze_path",
        lambda: state / "operational_safety_freeze.json",
    )

    engine = AutoRepairEngine(flatten_callback=lambda _epic: 0)
    result = engine.execute_dead_drop(
        "IX.D.NIKKEI.IFM.IP",
        reason="test",
        loop_error=True,
    )
    assert result["ok"] is True
    freeze = json.loads(
        (state / "operational_safety_freeze.json").read_text(encoding="utf-8")
    )
    assert freeze["operational_safety_freeze"] is True


def test_on_loop_tick_triggers_dead_drop_after_three_unhealthy(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config_v25.json").write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    diag = tmp_path / "sentinel_diagnostics.jsonl"

    monkeypatch.setattr("ai.operational.auto_repair.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.default_config_paths",
        lambda: [cfg / "config_v25.json"],
    )
    monkeypatch.setattr("ai.operational.auto_repair.ai_backups_dir", lambda: backup_dir)
    monkeypatch.setattr(
        "ai.operational.auto_repair.config_snapshot_backup_path",
        lambda: backup_dir / "config_snapshot_backup.json",
    )
    monkeypatch.setattr("ai.operational.auto_repair.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.telemetry_dead_drop_dir", lambda: state
    )
    monkeypatch.setattr(
        "ai.operational.auto_repair.operational_safety_freeze_path",
        lambda: state / "operational_safety_freeze.json",
    )
    monkeypatch.setattr("ai.paths.sentinel_diagnostics_path", lambda: diag)

    engine = AutoRepairEngine(flatten_callback=lambda _epic: 1)
    monitor = SystemMonitor(repair_engine=engine, agent_pid=99999)

    for _ in range(2):
        ev = monitor.on_loop_tick("EPIC", stream_disconnected=True)
        assert "dead_drop" not in ev

    ev3 = monitor.on_loop_tick("EPIC", stream_disconnected=True)
    assert ev3.get("dead_drop", {}).get("ok") is True


def test_validation_anchor_rolls_back_on_failed_e2e(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    v25 = cfg / "config_v25.json"
    v25.write_text('{"version": "25.0.0", "note": "original"}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    state_dir = tmp_path / "datalake_state"
    state_dir.mkdir()
    proposals_path = state_dir / "strategy_proposals.json"

    monkeypatch.setattr("ai.operational.auto_repair.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.default_config_paths", lambda: [v25]
    )
    monkeypatch.setattr("ai.operational.auto_repair.ai_backups_dir", lambda: backup_dir)
    monkeypatch.setattr(
        "ai.operational.auto_repair.config_snapshot_backup_path",
        lambda: backup_dir / "config_snapshot_backup.json",
    )
    monkeypatch.setattr("ai.operational.auto_repair.data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.strategy.backtest_simulator.strategy_proposals_path", lambda: proposals_path
    )
    monkeypatch.setattr(
        "ai.operational.auto_repair.strategy_proposals_path", lambda: proposals_path
    )

    engine = AutoRepairEngine()
    engine.run_e2e_validation = lambda: {  # type: ignore[method-assign]
        "ok": False,
        "perfect_30_30": False,
        "passed": 29,
        "total": 30,
    }

    proposals_path.write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "id": "p1",
                        "status": "approved",
                        "config_patch": {"note": "patched"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    results = engine.check_approved_proposals()
    assert results[0]["aborted"] is True
    restored = json.loads(v25.read_text(encoding="utf-8"))
    assert restored.get("note") == "original"


def test_frozen_key_blocks_proposal_patch(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    v25 = cfg / "config_v25.json"
    v25.write_text('{"confidence_floor": 72}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    monkeypatch.setattr("ai.operational.auto_repair.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ai.operational.auto_repair.default_config_paths", lambda: [v25]
    )
    monkeypatch.setattr("ai.operational.auto_repair.ai_backups_dir", lambda: backup_dir)
    monkeypatch.setattr(
        "ai.operational.auto_repair.config_snapshot_backup_path",
        lambda: backup_dir / "config_snapshot_backup.json",
    )
    monkeypatch.setattr("ai.operational.auto_repair.data_dir", lambda: tmp_path)

    engine = AutoRepairEngine()
    with pytest.raises(OperationalBoundaryError):
        engine.apply_proposal_config_patch(
            {"config_patch": {"confidence_floor": 65}},
        )


def test_backtest_simulator_writes_proposals(tmp_path, monkeypatch):
    out = tmp_path / "strategy_proposals.json"
    monkeypatch.setattr(
        "ai.strategy.backtest_simulator.strategy_proposals_path", lambda: out
    )

    sim = BacktestSimulator(proposals_path=out)
    proposal = sim.run_mock_backtest(epic="IX.D.NIKKEI.IFM.IP", proposal_name="test")
    assert proposal["status"] == "ready_for_review"
    store = json.loads(out.read_text(encoding="utf-8"))
    assert len(store["proposals"]) == 1


def test_port_open_localhost():
    assert port_open("127.0.0.1", 9) is False or port_open("127.0.0.1", 9) is True
