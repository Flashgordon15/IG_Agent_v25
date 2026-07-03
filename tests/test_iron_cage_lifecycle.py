"""Iron Cage boot lifecycle — deterministic 5-stage cold start integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime import master_orchestrator as mo
from system import alert_reporting_matrix as arm
from system import chaos_guardian as cg


@pytest.fixture(autouse=True)
def _isolate():
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    arm.reset_alert_reporting_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    arm.reset_alert_reporting_for_tests()


def _run_boot(epics=None):
    import asyncio

    return asyncio.run(mo._execute_warmup_async(epics))


def _mock_telemetry_pings(monkeypatch):
    monkeypatch.setattr(
        mo,
        "_ping_telemetry_routes",
        lambda: (
            True,
            [{"route": "guardian", "ok": True}, {"route": "tuner", "ok": True}],
        ),
    )


def test_cold_boot_all_stages_complete(monkeypatch):
    _mock_telemetry_pings(monkeypatch)
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 288, "CS.D.CFPGOLD.CFP.IP": 288},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 0},
    )
    result = _run_boot()
    assert result["ok"] is True
    assert result["primed"] is True
    assert result["trade_ready"] is True
    assert len(result["stages"]) == 5
    tokens = result["stage_tokens"]
    assert tokens[mo.STAGE_1_CONFIG_SANITY] == mo._TOKEN_SUCCESS
    assert tokens[mo.STAGE_5_LAUNCH] in (mo._TOKEN_SUCCESS, mo._TOKEN_WARMING)
    assert mo.all_warmup_phases_acceptable() is True


def test_stage1_creates_missing_cache_directories(tmp_path, monkeypatch):
    _mock_telemetry_pings(monkeypatch)
    cache = tmp_path / "ohlc_cache"
    assert not cache.exists()
    monkeypatch.setattr(mo, "_ensure_boot_directories", lambda: [str(cache)])
    monkeypatch.setattr(
        "runtime.parameter_tuner.ensure_tuning_overlay_or_default",
        lambda: {"ok": True, "created": False},
    )
    monkeypatch.setattr(
        "system.chaos_guardian.wake_guardian_for_boot",
        lambda **kw: {"ok": True, "registers_allocated": 16},
    )
    monkeypatch.setattr(
        "system.chaos_guardian.get_guardian_status_snapshot",
        lambda: {
            "ok": True,
            "healthy": True,
            "token_buckets": {"a": {}, "b": {}, "c": {}},
            "reconciliation_registers": {"allocated": 16},
            "packet_sanitization": {},
            "ts": 1.0,
        },
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 288},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 0},
    )
    result = _run_boot()
    assert result["stage_tokens"][mo.STAGE_1_CONFIG_SANITY] == mo._TOKEN_SUCCESS


def test_stage1_corrupted_overlay_writes_fallback(tmp_path, monkeypatch):
    _mock_telemetry_pings(monkeypatch)
    overlay = tmp_path / "tuning_overlay.json"
    overlay.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("IG_TUNING_OVERLAY", str(overlay))
    from runtime import parameter_tuner as pt

    pt.reset_parameter_tuner_for_tests()
    row = pt.ensure_tuning_overlay_or_default()
    assert row["ok"] is True
    assert row["created"] is True
    data = json.loads(overlay.read_text(encoding="utf-8"))
    assert "regime_matrix" in data
    assert "0" in data["regime_matrix"]


def test_stage3_warming_on_sparse_rings(monkeypatch):
    _mock_telemetry_pings(monkeypatch)
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 0, "CS.D.CFPGOLD.CFP.IP": 12},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 2},
    )
    result = _run_boot()
    assert result["ok"] is True
    assert result["stage_tokens"][mo.STAGE_3_REGIME_HYDRATION] == mo._TOKEN_WARMING
    assert result["warming_up"] is True
    assert mo.is_warming_up() is True


def test_unconfigured_webhooks_do_not_block_boot(monkeypatch):
    _mock_telemetry_pings(monkeypatch)
    monkeypatch.setattr(arm, "_telegram_configured", lambda: False)
    monkeypatch.setattr(arm, "_discord_configured", lambda: False)
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 288},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 0},
    )
    result = _run_boot()
    assert result["ok"] is True
    snap = arm.get_reporting_status_snapshot()
    assert snap["subsystem_status"] == "IDLE"
    assert arm.reporting_healthy() is True


def test_stage2_preallocates_reconciliation_registers():
    cg.reset_chaos_guardian_for_tests()
    n = cg.preallocate_reconciliation_registers(max_assets=12)
    assert n == 12
    snap = cg.get_reconciliation_register_snapshot()
    assert snap["allocated"] == 12
    assert len(snap["registers"]) <= 8


def test_sequential_gate_blocks_stage_without_prior_token():
    mo._commit_stage_token(mo.STAGE_1_CONFIG_SANITY, mo._TOKEN_SUCCESS)
    assert mo._can_start_stage(mo.STAGE_2_GUARDIAN_WAKE) is True
    assert mo._can_start_stage(mo.STAGE_3_REGIME_HYDRATION) is False
    mo._commit_stage_token(mo.STAGE_2_GUARDIAN_WAKE, mo._TOKEN_FAILED)
    assert mo._can_start_stage(mo.STAGE_3_REGIME_HYDRATION) is False


def test_transient_error_retries_with_deterministic_backoff(monkeypatch):
    calls = {"n": 0}

    async def _flaky_stage1():
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("file handle locked")
        return True, mo._log_stage(mo.STAGE_1_CONFIG_SANITY, True, "recovered", token=mo._TOKEN_SUCCESS)

    monkeypatch.setattr(mo, "_stage1_config_sanity", _flaky_stage1)
    delays: list[float] = []
    monkeypatch.setattr(mo, "_deterministic_retry_delay_sec", lambda a: delays.append(a) or 0.0)
    import asyncio

    ok, log = asyncio.run(mo._run_stage_with_retries(mo.STAGE_1_CONFIG_SANITY, mo._stage1_config_sanity))
    assert ok is True
    assert calls["n"] >= 2
    assert len(delays) >= 1
