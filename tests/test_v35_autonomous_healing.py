"""v35 autonomous self-healing — REST rate-smoothing, cache purge, 30s boot buffer."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from system import agent_orchestration as ao


@pytest.fixture(autouse=True)
def _reset_orchestrator() -> None:
    ao.reset_orchestrator_for_tests()
    yield
    ao.reset_orchestrator_for_tests()


def test_rest_high_over_three_ticks_doubles_rest_poll_interval(monkeypatch) -> None:
    """REST HIGH for >3 consecutive ticks → executive back-off doubles rest_poll."""
    monkeypatch.setattr(
        ao,
        "_seed_rest_poll_intervals",
        lambda: {
            "rest_poll": "2s",
            "rest_min_interval_seconds": 2.0,
            "stream_poll_seconds": 8.0,
        },
    )
    monkeypatch.setattr(ao, "soft_flush_network_buffers", lambda: {"flushed": ["mock"]})
    monkeypatch.setattr(
        "system.rest_api_budget.configure_rest_api_budget",
        lambda **kwargs: MagicMock(),
    )

    for _ in range(3):
        out = ao.maybe_rest_pressure_executive_heal(
            pressure_level="HIGH",
            entries_paused=False,
        )
        assert out is None or "executive_rest_backoff" not in (out.get("actions") or [])

    result = ao.maybe_rest_pressure_executive_heal(
        pressure_level="REST_PRESSURE_HIGH",
        entries_paused=False,
    )
    assert result is not None
    assert "executive_rest_backoff" in result["actions"]
    intervals = ao.get_rest_poll_intervals()
    assert intervals["rest_poll"] == "4s"
    assert float(intervals["rest_min_interval_seconds"]) == pytest.approx(4.0)
    assert result["backoff"]["flush"]["flushed"] == ["mock"]


def test_rest_pressure_high_entries_paused_purges_and_reauths(
    tmp_path: Path, monkeypatch
) -> None:
    """REST_PRESSURE_HIGH + entries_paused → token/lock purge + soft-reauth."""
    monkeypatch.setattr(ao, "data_dir", lambda: tmp_path)
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)

    token = tmp_path / "ig_session_tokens_Z6BAH4.json"
    token.write_text('{"cst":"stale"}', encoding="utf-8")
    legacy_lock = tmp_path / ".ig_agent_v29.lock"
    legacy_lock.write_text("pid=1\n", encoding="utf-8")
    # Protected SoT must survive.
    sot = tmp_path / "trade_support_status.json"
    sot.write_text(json.dumps({"ts": time.time(), "broker_open": 0}), encoding="utf-8")
    learning = tmp_path / "learning.db"
    learning.write_text("keep", encoding="utf-8")

    mock_client = MagicMock()
    mock_client.refresh_session = MagicMock(return_value=True)
    mock_registry = MagicMock()
    mock_registry.get_client_for_account.return_value = mock_client

    monkeypatch.setattr(
        "runtime.session_registry.get_session_registry",
        lambda: mock_registry,
    )
    monkeypatch.setattr(
        "system.credentials_loader.try_load_credentials",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "runtime.session_lock.clear_stale_lock",
        lambda path: True,
    )
    monkeypatch.setattr(ao, "soft_flush_network_buffers", lambda: {"flushed": []})

    result = ao.maybe_rest_pressure_executive_heal(
        pressure_level="REST_PRESSURE_HIGH",
        entries_paused=True,
    )
    assert result is not None
    assert "cache_purge" in result["actions"]
    assert "soft_reauth" in result["actions"]
    assert "ig_session_tokens_Z6BAH4.json" in result["purge"]["tokens_removed"]
    assert ".ig_agent_v29.lock" in result["purge"]["locks_cleared"]
    assert any(s.startswith("protected:") for s in result["purge"]["skipped"])
    assert sot.is_file()
    assert learning.is_file()
    assert not token.exists()
    assert not legacy_lock.exists()
    mock_client.refresh_session.assert_called()
    assert result["reauth"]["ok"] is True
    assert result["reauth"]["path"] == "refresh_session"


def test_boot_hydration_uses_30s_latency_buffer_no_false_blockage(monkeypatch) -> None:
    """Boot/hydration window uses 30s relaxed latency buffer — no false ENGINE BLOCKAGE."""
    from runtime import desk_stability_harness as dsh

    prior_boot = dsh.boot_started_at()
    try:
        dsh.note_boot_started(time.time())
        conf = {
            "boot_grace_sec": 60.0,
            "boot_latency_buffer_sec": 30.0,
            "boot_trade_support_stale_sec": 30.0,
            "runtime_trade_support_stale_sec": 20.0,
        }
        assert dsh.boot_latency_buffer_sec(cfg=conf) == pytest.approx(30.0)
        assert dsh.boot_latency_buffer_active(cfg=conf) is True
        assert dsh.false_engine_blockage_suppressed(cfg=conf) is True
        assert dsh.trade_support_stale_budget_sec(cfg=conf) == pytest.approx(30.0)

        # Outside buffer → suppression lifts.
        dsh.note_boot_started(time.time() - 31.0)
        assert dsh.boot_latency_buffer_active(cfg=conf) is False
        assert dsh.false_engine_blockage_suppressed(cfg=conf) is False

        # Config overlay ships 30s buffer.
        cfg_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "config_v31_demo_throughput.json"
        )
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        harness = raw.get("desk_stability_harness") or {}
        assert float(harness.get("boot_latency_buffer_sec")) == pytest.approx(30.0)
        assert float(harness.get("boot_trade_support_stale_sec")) == pytest.approx(30.0)
    finally:
        dsh.note_boot_started(prior_boot)


def test_after_healing_restores_emerald_operational_status(monkeypatch) -> None:
    """After healing actions, mock stability returns operational / not hold."""
    monkeypatch.setattr(ao, "soft_flush_network_buffers", lambda: {"flushed": ["x"]})
    monkeypatch.setattr(
        "system.rest_api_budget.configure_rest_api_budget",
        lambda **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        ao,
        "_seed_rest_poll_intervals",
        lambda: {"rest_poll": "5s", "rest_min_interval_seconds": 5.0},
    )
    monkeypatch.setattr(ao, "purge_stale_token_and_lock_caches", lambda: {"tokens_removed": []})
    monkeypatch.setattr(
        ao,
        "soft_reauth_session",
        lambda: {"ok": True, "path": "refresh_session"},
    )

    for _ in range(4):
        ao.maybe_rest_pressure_executive_heal(
            pressure_level="HIGH",
            entries_paused=False,
        )
    assert ao.get_orchestrator_status()["operational_status"] == "healing"
    assert ao.get_orchestrator_status()["healing_active"] is True

    restored = ao.maybe_rest_pressure_executive_heal(
        pressure_level="IDLE",
        entries_paused=False,
    )
    assert restored is not None
    assert "operational_restored" in restored["actions"]
    assert restored["restored"]["status"] == "operational"
    assert restored["restored"]["desk_rag"] == "G"
    assert restored["restored"]["hold"] is False
    assert restored["restored"]["healing_active"] is False

    status = ao.get_orchestrator_status()
    assert status["operational_status"] == "operational"
    assert status["healing_active"] is False
    assert status["rest_high_consecutive"] == 0


def test_double_interval_value_handles_string_and_numeric() -> None:
    assert ao.double_interval_value("2s") == "4s"
    assert ao.double_interval_value("5s") == "10s"
    assert ao.double_interval_value("500ms") == "1000ms"
    assert ao.double_interval_value(20.0) == pytest.approx(40.0)
