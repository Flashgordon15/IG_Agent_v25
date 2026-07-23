"""Unit tests for v33 agent_orchestration — classifier + cooldown (mocked heal)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from system import agent_orchestration as ao


@pytest.fixture(autouse=True)
def _reset_orchestrator() -> None:
    ao.reset_orchestrator_for_tests()
    yield
    ao.reset_orchestrator_for_tests()


def test_classify_stale_lock_collision() -> None:
    lines = [
        "session_lock: preflight failed",
        "session already active for account_scope=ig:Z6BAH4 (pid=999)",
    ]
    fault = ao.classify_log_lines(lines, engine="cfd", port=8080, health_ok=False)
    assert fault is not None
    assert fault.classification == ao.FaultClass.STALE_LOCK_COLLISION
    assert fault.port == 8080
    assert fault.recommended_plan


def test_classify_http_429() -> None:
    lines = ["ig_rest: HTTP 429 Too Many Requests on /positions"]
    fault = ao.classify_log_lines(lines, engine="sb", port=8081, health_ok=True)
    assert fault is not None
    assert fault.classification == ao.FaultClass.HTTP_429


def test_classify_stage_4_cliff() -> None:
    lines = ["2026-07-22 pid=1 STAGE_4_TUNER_PRIME EXIT_FAIL cliff timeout"]
    fault = ao.classify_log_lines(lines, engine="cfd", port=8080, health_ok=False)
    assert fault is not None
    assert fault.classification == ao.FaultClass.STAGE_4_BOOT_CLIFF


def test_classify_port_offline_when_health_down_and_no_pattern() -> None:
    fault = ao.classify_log_lines([], engine="sb", port=8081, health_ok=False)
    assert fault is not None
    assert fault.classification == ao.FaultClass.PORT_OFFLINE


def test_heal_cooldown_max_two_per_window() -> None:
    base = 1000.0
    assert ao.heal_cooldown_allows_attempt(base) is True
    ao.record_heal_attempt(base)
    assert ao.heal_cooldown_allows_attempt(base + 1) is True
    ao.record_heal_attempt(base + 2)
    assert ao.heal_cooldown_allows_attempt(base + 3) is False
    assert ao.heal_cooldown_allows_attempt(base + ao.HEAL_WINDOW_SEC + 1) is True


def test_should_run_orchestrator_dual_cfd_only(monkeypatch) -> None:
    monkeypatch.delenv("IG_AGENT_PYTEST", raising=False)
    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")
    monkeypatch.setenv("IG_API_PORT", "8080")
    monkeypatch.delenv("IG_AGENT_ORCHESTRATOR", raising=False)
    assert ao.should_run_orchestrator() is True
    monkeypatch.setenv("IG_API_PORT", "8081")
    assert ao.should_run_orchestrator() is False


def test_should_run_orchestrator_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("IG_AGENT_ORCHESTRATOR", "1")
    monkeypatch.delenv("IG_V32_DUAL_PORT", raising=False)
    assert ao.should_run_orchestrator() is True


def test_attempt_restore_aborts_on_broker_opens() -> None:
    fault = ao.FaultReport(
        classification=ao.FaultClass.ENGINE_DROP,
        engine="sb",
        port=8081,
        detail="test",
    )
    with patch.object(ao, "_broker_opens_count", return_value=2):
        result = ao.attempt_allowlisted_restore(
            cfd_online=True,
            sb_online=False,
            fault=fault,
        )
    assert result["attempted"] is False
    assert "broker_opens" in result["reason"]


def test_attempt_restore_respects_cooldown() -> None:
    import time

    fault = ao.FaultReport(
        classification=ao.FaultClass.PORT_OFFLINE,
        engine="sb",
        port=8081,
        detail="test",
    )
    base = time.monotonic()
    ao.record_heal_attempt(base)
    ao.record_heal_attempt(base + 1)
    with patch.object(ao, "_broker_opens_count", return_value=0):
        with patch.object(ao, "_launch_single_engine", return_value=None):
            result = ao.attempt_allowlisted_restore(
                cfd_online=True,
                sb_online=False,
                fault=fault,
            )
    assert result["reason"] == "heal_cooldown_exhausted"


def test_write_diagnostics_fault_skipped_under_pytest(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    report = ao.FaultReport(
        classification=ao.FaultClass.STALE_LOCK_COLLISION,
        engine="cfd",
        port=8080,
        detail="lock",
    )
    ao._write_diagnostics_fault(report)
    assert not (tmp_path / "diagnostics_fault.json").exists()


def test_get_orchestrator_status_shape() -> None:
    status = ao.get_orchestrator_status()
    assert status["ok"] is True
    assert "healing_active" in status
    assert "dual_engine_operational" in status
    assert status["heal_max_attempts"] == ao.HEAL_MAX_ATTEMPTS


def test_engine_operational_trading_healthy_despite_ok_false() -> None:
    health = {
        "online": True,
        "ok": False,
        "status": 200,
        "payload": {
            "ok": False,
            "trading_healthy": True,
            "trade_ready": True,
            "issues": ["watchdog_inactive"],
        },
    }
    assert ao._engine_operational(health) is True


def test_engine_operational_offline_on_connection_refused() -> None:
    health = {"online": False, "ok": False, "error": "Connection refused"}
    assert ao._engine_operational(health) is False


def test_engine_operational_http_non_200() -> None:
    health = {"online": True, "ok": False, "status": 503, "payload": {}}
    assert ao._engine_operational(health) is False


def test_watchdog_only_unhealthy_with_v32_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    marker = tmp_path / "state"
    marker.mkdir(parents=True)
    (marker / "v32_dual_supervision.json").write_text("{}", encoding="utf-8")
    payload = {
        "ok": False,
        "trading_healthy": True,
        "issues": ["watchdog_inactive"],
        "supervision_drift": {"issues": ["agent_running_without_watchdog"]},
    }
    assert ao._watchdog_only_unhealthy(payload) is True


def test_tick_once_clears_orchestrator_deploy_hold(tmp_path: Path, monkeypatch) -> None:
    from runtime import deploy_hold

    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "v32_dual_supervision.json").write_text("{}", encoding="utf-8")
    deploy_hold.set_deploy_hold(active=True, reason="orchestrator:port_offline")

    healthy_payload = {
        "ok": False,
        "trading_healthy": True,
        "trade_ready": True,
        "issues": ["watchdog_inactive"],
    }
    poll = {
        "online": True,
        "ok": False,
        "status": 200,
        "payload": healthy_payload,
        "operational": True,
    }

    with patch.object(ao, "_poll_health", return_value=poll):
        ao._tick_once()

    hold = deploy_hold._read_hold_file()
    assert hold.get("active") is False
    assert str(hold.get("reason", "")).startswith("orchestrator:cleared")


def test_tick_once_skips_deploy_hold_on_watchdog_only_drift(
    tmp_path: Path, monkeypatch
) -> None:
    from runtime import deploy_hold

    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "v32_dual_supervision.json").write_text("{}", encoding="utf-8")
    deploy_hold.set_deploy_hold(active=False, reason="operator")

    payload = {
        "ok": False,
        "trading_healthy": False,
        "issues": ["watchdog_inactive"],
        "supervision_drift": {"issues": ["agent_running_without_watchdog"]},
    }
    cfd_poll = {
        "online": True,
        "ok": False,
        "status": 200,
        "payload": payload,
        "operational": False,
    }
    sb_poll = {
        "online": True,
        "ok": True,
        "status": 200,
        "payload": {"ok": True, "trading_healthy": True},
        "operational": True,
    }

    with patch.object(ao, "_poll_health", side_effect=[cfd_poll, sb_poll]):
        with patch.object(ao, "_mark_fail_closed") as mark_mock:
            ao._tick_once()
            mark_mock.assert_not_called()


def test_mark_fail_closed_skips_watchdog_only_under_v32(tmp_path: Path, monkeypatch) -> None:
    from runtime import deploy_hold

    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "v32_dual_supervision.json").write_text("{}", encoding="utf-8")
    deploy_hold.set_deploy_hold(active=False, reason="operator")

    payload = {
        "trading_healthy": True,
        "issues": ["watchdog_inactive"],
        "supervision_drift": {"issues": ["agent_running_without_watchdog"]},
    }
    ao._mark_fail_closed("port_offline", health_payload=payload)
    assert deploy_hold._read_hold_file().get("active") is False


def test_resolve_engine_log_paths_prefers_existing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "v31-production"
    log_dir = root / "logs"
    log_dir.mkdir(parents=True)
    cfd_log = log_dir / "v32_cfd.log"
    cfd_log.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(ao, "data_dir", lambda: root)
    paths = ao.resolve_engine_log_paths()
    assert paths["cfd"] == cfd_log


def test_tick_once_skips_heal_during_boot_grace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    sb_log = log_dir / "v32_sb.log"
    sb_log.write_text(
        "2026-07-22 | MasterOrchestrator: STAGE_6_REST_AUTH RUN attempt=1\n",
        encoding="utf-8",
    )

    boot_payload = {
        "ok": False,
        "trading_healthy": False,
        "boot_metrics": {"ready": False},
        "system_state": {"phase": "G2", "ready": False, "started_at_epoch": time.time()},
    }
    cfd_poll = {
        "online": True,
        "ok": True,
        "status": 200,
        "payload": {"ok": True, "trading_healthy": True},
        "operational": True,
    }
    sb_poll = {
        "online": True,
        "ok": False,
        "status": 200,
        "payload": boot_payload,
        "operational": False,
    }

    with patch.object(ao, "_poll_health", side_effect=[cfd_poll, sb_poll]):
        with patch.object(ao, "_handle_fault") as handle_mock:
            ao._tick_once()
            handle_mock.assert_not_called()


def test_boot_stale_sot_cap_breach_override_clears_flat_pause(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    state = tmp_path / "state_cfd"
    state.mkdir(parents=True)
    (state / "trading_paused.json").write_text(
        json.dumps(
            {
                "active": True,
                "reason": "stability_harness_cap_breach",
                "ts": time.time(),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "trade_support_status.json").write_text(
        json.dumps({"ts": time.time() - 20.5, "broker_open": 0}),
        encoding="utf-8",
    )

    monkeypatch.setattr(ao, "_broker_opens_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(ao, "_real_cap_breach_live", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "runtime.desk_stability_harness.boot_grace_active",
        lambda **_: True,
    )
    monkeypatch.setattr(
        ao,
        "_orchestrator_boot_elapsed_sec",
        lambda: 5.0,
    )
    monkeypatch.setattr(
        "system.startup_hold_clear.book_flat_via_api",
        lambda *_a, **_k: True,
    )

    result = ao._maybe_boot_stale_sot_cap_breach_override()
    assert result is not None
    assert result["event"] == "boot_stale_sot_cap_breach_override"
    raw = json.loads((state / "trading_paused.json").read_text(encoding="utf-8"))
    assert raw.get("active") is False


def test_boot_stale_sot_override_skips_real_cap_breach(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "trading_paused.json").write_text(
        json.dumps({"active": True, "reason": "stability_harness_cap_breach"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ao, "_real_cap_breach_live", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "runtime.desk_stability_harness.boot_grace_active",
        lambda **_: True,
    )
    assert ao._maybe_boot_stale_sot_cap_breach_override() is None


def test_orchestrator_heal_pause_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    assert ao.orchestrator_heal_paused() is False
    ao.set_orchestrator_heal_pause(active=True, reason="boot_recovery")
    assert ao.orchestrator_heal_paused() is True
    with patch.object(ao, "_poll_health") as poll_mock:
        ao._tick_once()
        poll_mock.assert_not_called()
    ao.set_orchestrator_heal_pause(active=False)
    assert ao.orchestrator_heal_paused() is False


def test_api_desk_orchestrator_route() -> None:
    from api.routes import api_desk_orchestrator

    body = api_desk_orchestrator()
    assert body.get("ok") is True
    assert "healing_active" in body
