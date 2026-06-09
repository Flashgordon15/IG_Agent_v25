"""Tests for v28 CIAO — profiler, staging envelope, verification gateway."""

from __future__ import annotations

import json
import time

import pytest

from ai.operational.auto_repair import AutoRepairEngine
from ai.operational.profiler import OperationalProfiler
from ai.staging.envelope import (
    StagingBoundaryError,
    StagingEnvelope,
    assert_staging_may_target,
)


def test_rolling_percentiles_p50_p95_p99():
    prof = OperationalProfiler(window_sec=3600.0)
    for ms in (10.0, 20.0, 30.0, 40.0, 100.0):
        prof.record_probe("probe_trading_loop_tick", ms, epic="EPIC")
    stats = prof.rolling_percentiles("probe_trading_loop_tick")
    probe = stats["probes"]["probe_trading_loop_tick"]
    assert probe["n"] == 5
    assert probe["p50_ms"] == pytest.approx(30.0)
    assert probe["p95_ms"] >= 40.0
    assert probe["p99_ms"] >= 40.0


def test_inactivity_investigator_writes_rca(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    rca_dir = state / "rca_diagnostics"
    rca_dir.mkdir()
    sentinel = state / "sentinel_diagnostics.jsonl"
    sentinel.write_text(
        json.dumps({"epic": "EPIC", "unhealthy": False, "type": "loop_tick"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("ai.operational.profiler.rca_diagnostics_dir", lambda: rca_dir)
    monkeypatch.setattr(
        "ai.operational.profiler.sentinel_diagnostics_path", lambda: sentinel
    )
    monkeypatch.setattr(
        "ai.operational.profiler.operational_safety_freeze_path",
        lambda: tmp_path / "missing_freeze.json",
    )

    prof = OperationalProfiler(inactivity_window_sec=0.0)
    prof.update_session_activity(
        "EPIC",
        session_open=True,
        trade_executed=False,
        atr_filter_cleared=True,
        gate_failures={"signal_confidence": 5},
        dominant_gate_block="signal_confidence",
    )
    prof._sessions["EPIC"].session_open_since = time.time() - 3600

    result = prof.investigate_inactivity("EPIC")
    assert result is not None
    assert result["payload"]["type"] == "RCA_DIAGNOSTIC"
    assert result["payload"]["dominant_gate_block"] == "signal_confidence"
    assert list(rca_dir.glob("rca_*.json"))


def test_staging_write_barrier_blocks_trading_runtime():
    with pytest.raises(StagingBoundaryError):
        assert_staging_may_target("src/trading/trading_loop.py")
    with pytest.raises(StagingBoundaryError):
        assert_staging_may_target("src/runtime/market_orchestrator.py")


def test_promote_staged_optimization_rolls_back_on_failed_e2e(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    v25 = cfg / "config_v25.json"
    v25.write_text('{"version": "25.0.0", "note": "original"}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

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
    monkeypatch.setattr("ai.staging.envelope.project_root", lambda: tmp_path)
    monkeypatch.setattr("ai.staging.envelope.staging_root", lambda: staging)

    envelope = StagingEnvelope(root=staging)
    opt_id = "opt-test-1"
    opt_dir = envelope.optimization_dir(opt_id)
    patch_dir = opt_dir / "patch"
    patch_file = patch_dir / "config_v25.json"
    patch_file.write_text('{"note": "staged"}\n', encoding="utf-8")
    envelope.create_manifest(
        opt_id,
        patches=[
            {
                "relative_target": "config/config_v25.json",
                "patch_file": "patch/config_v25.json",
            }
        ],
    )

    engine = AutoRepairEngine()
    engine.run_e2e_validation = lambda: {  # type: ignore[method-assign]
        "ok": False,
        "perfect_30_30": False,
        "passed": 29,
        "total": 30,
    }

    result = engine.promote_staged_optimization(opt_id)
    assert result["ok"] is False
    assert result["warning_code"] == "aborted_verification_failed"
    restored = json.loads(v25.read_text(encoding="utf-8"))
    assert restored.get("note") == "original"
    manifest = envelope.load_manifest(opt_id)
    assert manifest["status"] == "aborted_verification_failed"


def test_promote_rejects_frozen_config_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    v25 = cfg / "config_v25.json"
    v25.write_text('{"confidence_floor": 72}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

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
    monkeypatch.setattr("ai.staging.envelope.project_root", lambda: tmp_path)
    monkeypatch.setattr("ai.staging.envelope.staging_root", lambda: staging)

    envelope = StagingEnvelope(root=staging)
    opt_id = "opt-frozen-block"
    opt_dir = envelope.optimization_dir(opt_id)
    patch_file = opt_dir / "patch" / "config_v25.json"
    patch_file.write_text('{"confidence_floor": 59}\n', encoding="utf-8")
    envelope.create_manifest(
        opt_id,
        patches=[
            {
                "relative_target": "config/config_v25.json",
                "patch_file": "patch/config_v25.json",
            }
        ],
    )

    engine = AutoRepairEngine()
    result = engine.promote_staged_optimization(opt_id)
    assert result["ok"] is False
    assert "frozen key" in str(result.get("reason", "")).lower()
    assert json.loads(v25.read_text())["confidence_floor"] == 72
