"""Database backup daemon and 7-day PP trajectory integration tests."""

from __future__ import annotations

import sqlite3
import tarfile
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_backup_analytics_state():
    import runtime.master_orchestrator as mo
    import system.chaos_guardian as cg
    from analytics.historical_analyzer import reset_pp_history_for_tests
    from system.backup_manager import reset_backup_manager_for_tests

    reset_backup_manager_for_tests()
    reset_pp_history_for_tests()
    cg.reset_backup_compliance_for_tests()
    mo.reset_master_orchestrator_for_tests()
    yield
    reset_backup_manager_for_tests()
    reset_pp_history_for_tests()
    cg.reset_backup_compliance_for_tests()
    mo.reset_master_orchestrator_for_tests()


def _seed_triage_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS closed_positions (
                id INTEGER PRIMARY KEY,
                epic TEXT,
                pnl_gbp REAL,
                closed_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO closed_positions (epic, pnl_gbp, closed_at) VALUES (?, ?, ?)",
            ("CS.D.EURUSD.CFD.IP", 12.5, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def test_pp_trajectory_seven_day_synthetic_variation():
    from analytics.historical_analyzer import get_pp_trajectory_7d, simulate_seven_day_pp_variation

    traj = simulate_seven_day_pp_variation([980, 1020, 1080, 1150, 1210, 1250, 1280])
    assert traj.get("ok") is True
    assert len(traj.get("pp_scores") or []) == 7
    assert traj.get("trend") == "expansion"
    assert get_pp_trajectory_7d()["count"] == 7


def test_pp_trajectory_defense_contraction():
    from analytics.historical_analyzer import simulate_seven_day_pp_variation

    traj = simulate_seven_day_pp_variation([1100, 1050, 980, 920, 860, 820, 780])
    assert traj.get("trend") == "defense"


def test_pp_trajectory_in_iron_ledger_publish():
    import system.chaos_guardian as cg
    from analytics.historical_analyzer import get_pp_trajectory_7d, simulate_seven_day_pp_variation

    simulate_seven_day_pp_variation([1000, 1040, 1080, 1120, 1160, 1200, 1240])
    traj = get_pp_trajectory_7d()
    cg.IronLedgerSnapshot.commit(
        {
            "ts": time.time(),
            "platform_pp": 1240,
            "pp_trajectory_7d": traj,
            "orchestrator": {"ok": True},
            "guardian": {"ok": True},
        }
    )
    ledger = cg.IronLedgerSnapshot.read()
    assert (ledger.get("pp_trajectory_7d") or {}).get("ok") is True
    assert len((ledger.get("pp_trajectory_7d") or {}).get("pp_scores") or []) >= 2


def test_daily_backup_creates_tarball(tmp_path, monkeypatch):
    import system.chaos_guardian as cg
    from system.backup_manager import backup_archive_dir, execute_daily_database_backup

    triage = tmp_path / "triage.db"
    overlay = tmp_path / "tuning_overlay.json"
    _seed_triage_db(triage)
    overlay.write_text('{"params": {}}', encoding="utf-8")

    monkeypatch.setattr("system.backup_manager._tuning_overlay_path", lambda: overlay)
    monkeypatch.setattr("system.paths.triage_db_path", lambda: triage)
    monkeypatch.setattr("system.backup_manager.backup_archive_dir", lambda: tmp_path / "backups")
    monkeypatch.setenv("IG_BACKUP_INTERVAL_SEC", "1")

    result = execute_daily_database_backup(force=True)
    assert result.get("ok") is True
    archive = Path(str(result.get("archive") or ""))
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "triage.db" in names
    assert "config/tuning_overlay.json" in names

    compliance = cg.build_guardian_snapshot_body().get("database_backup_compliance") or []
    assert any(row.get("ok") for row in compliance)


def test_backup_skips_when_interval_not_elapsed(tmp_path, monkeypatch):
    from system.backup_manager import execute_daily_database_backup, reset_backup_manager_for_tests

    triage = tmp_path / "triage.db"
    _seed_triage_db(triage)
    monkeypatch.setattr("system.paths.triage_db_path", lambda: triage)
    monkeypatch.setattr("system.backup_manager.backup_archive_dir", lambda: tmp_path / "backups")
    monkeypatch.setenv("IG_BACKUP_INTERVAL_SEC", "86400")

    first = execute_daily_database_backup(force=True)
    assert first.get("ok") is True
    second = execute_daily_database_backup(force=False)
    assert second.get("skipped") is True


def test_backup_under_file_lock_contention(tmp_path, monkeypatch):
    from system.backup_manager import execute_daily_database_backup

    triage = tmp_path / "triage.db"
    _seed_triage_db(triage)
    monkeypatch.setattr("system.paths.triage_db_path", lambda: triage)
    monkeypatch.setattr("system.backup_manager.backup_archive_dir", lambda: tmp_path / "backups")

    release = threading.Event()
    outcome: dict[str, object] = {}

    def _hold_lock():
        conn = sqlite3.connect(str(triage), timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            release.wait(timeout=3.0)
        finally:
            conn.rollback()
            conn.close()

    t = threading.Thread(target=_hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)
    outcome["result"] = execute_daily_database_backup(force=True)
    release.set()
    t.join(timeout=3.0)
    result = outcome.get("result") or {}
    assert isinstance(result, dict)
    assert result.get("ok") is True or result.get("triage_included") is False


def test_ai_diagnostics_surfaces_pp_trajectory(monkeypatch):
    import system.autonomic_healer as ah
    from analytics.historical_analyzer import simulate_seven_day_pp_variation
    from system.autonomic_healer import get_ai_diagnostics_snapshot
    from system.chaos_guardian import IronLedgerSnapshot

    monkeypatch.setattr(ah, "_refresh_snapshot", lambda: None)
    with ah._lock:
        ah._snapshot["ts"] = time.time()

    traj = simulate_seven_day_pp_variation([1000, 1100, 1150, 1180, 1220, 1260, 1300])
    IronLedgerSnapshot.commit(
        {
            "ts": time.time(),
            "platform_pp": 1300,
            "pp_trajectory_7d": traj,
            "orchestrator": {"ok": True},
            "guardian": {"ok": True},
        }
    )
    diag = get_ai_diagnostics_snapshot()
    assert (diag.get("pp_trajectory_7d") or {}).get("ok") is True
    assert (diag.get("pp_trajectory_7d") or {}).get("latest_pp", 0) >= 1200
