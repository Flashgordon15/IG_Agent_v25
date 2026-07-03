"""Master orchestrator — warmup, routing, gamified scoreboard tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runtime import master_orchestrator as mo


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("IG_STABILIZER_SKIP", "1")
    mo.reset_master_orchestrator_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()


def test_platform_scoreboard_baseline():
    sb = mo.PlatformScoreboard()
    assert sb.total_pp == mo.BASE_PERFORMANCE_POINTS
    assert sb.rank_label() == "standard"


def test_scoreboard_awards_full_target_zero_slip():
    sb = mo.PlatformScoreboard()
    row = sb.record_trade_outcome(hit_full_target=True, zero_slippage=True)
    assert row["delta"] == mo.PP_FULL_TARGET_ZERO_SLIP
    assert sb.total_pp == mo.BASE_PERFORMANCE_POINTS + mo.PP_FULL_TARGET_ZERO_SLIP


def test_scoreboard_penalizes_stop_loss():
    sb = mo.PlatformScoreboard()
    sb.record_trade_outcome(hit_stop=True)
    assert sb.total_pp == mo.BASE_PERFORMANCE_POINTS - mo.PP_STOP_OR_SLIP_PENALTY


def test_scoreboard_win_rate_bonus():
    sb = mo.PlatformScoreboard()
    for _ in range(mo.WIN_RATE_WINDOW):
        sb.record_trade_outcome(won=True)
    assert sb.rolling_win_rate() >= mo.WIN_RATE_TARGET
    pp_before = sb.total_pp
    sb.record_trade_outcome(won=True)
    assert sb.total_pp >= pp_before


def test_defensive_contraction_below_800():
    sb = mo.PlatformScoreboard()
    sb.total_pp = 750
    assert sb.size_factor_multiplier() == 0.50
    assert sb.capacity_multiplier() == 0.75


def test_expansion_above_1200():
    sb = mo.PlatformScoreboard()
    sb.total_pp = 1400
    assert sb.size_factor_multiplier() > 1.0
    assert sb.capacity_multiplier() > 1.0


def _mock_full_boot(monkeypatch):
    monkeypatch.setattr(
        mo,
        "_ping_telemetry_routes",
        lambda: (True, [{"route": "guardian", "ok": True}]),
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 288, "CS.D.CFPGOLD.CFP.IP": 200},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 0},
    )


def test_warmup_phase2_degraded_on_sparse_rings(monkeypatch):
    _mock_full_boot(monkeypatch)
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {"IX.D.DOW.IFM.IP": 0, "CS.D.CFPGOLD.CFP.IP": 12},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 2, "hub_seed_count": 0},
    )
    import asyncio

    result = asyncio.run(mo._execute_warmup_async())
    assert result["ok"] is True
    assert result["primed"] is True
    assert result["stage_status"][mo.STAGE_3_REGIME_HYDRATION] == mo.RAG_RUNNING
    assert result["warming_up"] is True
    assert mo.all_warmup_phases_acceptable() is True
    assert mo.is_warming_up() is True


def test_degraded_override_snapshot():
    for stage in mo._BOOT_STAGES:
        token = mo._TOKEN_WARMING if stage == mo.STAGE_3_REGIME_HYDRATION else mo._TOKEN_SUCCESS
        mo._commit_stage_token(stage, token)
    mo._primed = True
    mo._boot_trade_ready = True
    mo._armed = True
    mo._refresh_snapshot()
    mo.publish_iron_ledger_snapshot()
    snap = mo.get_orchestrator_state_snapshot()
    assert snap["warming_up"] is True
    assert snap["healthy"] is True
    assert snap["degraded_override"] is True
    assert snap["fully_green"] is False


def test_warmup_phases(monkeypatch):
    _mock_full_boot(monkeypatch)
    import asyncio

    result = asyncio.run(mo._execute_warmup_async())
    assert result["ok"] is True
    assert result["primed"] is True
    assert len(result["stages"]) == len(mo._BOOT_STAGES)
    assert mo.is_orchestrator_primed() is True
    assert mo.all_warmup_phases_acceptable() is True
    assert result["trade_ready"] is True


def test_warmup_retries_on_transient_error(monkeypatch):
    calls = {"n": 0}

    async def _flaky_stage1():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("websocket not ready")
        return True, mo._log_stage(mo.STAGE_1_CONFIG_SANITY, True, "recovered", token=mo._TOKEN_SUCCESS)

    monkeypatch.setattr(mo, "_stage1_config_sanity", _flaky_stage1)

    async def _stage2():
        return True, mo._log_stage(mo.STAGE_2_GUARDIAN_WAKE, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage3(epics=None):
        return True, mo._log_stage(mo.STAGE_3_REGIME_HYDRATION, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage4():
        return True, mo._log_stage(mo.STAGE_4_TUNER_PRIME, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage5():
        return True, mo._log_stage(mo.STAGE_5_LAUNCH_CORE, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage6():
        return True, mo._log_stage(mo.STAGE_6_REST_AUTH, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage7():
        return True, mo._log_stage(mo.STAGE_7_STREAM_HANDSHAKE, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage8(epics=None):
        return True, mo._log_stage(mo.STAGE_8_DATA_FEED_HYDRATION, True, "ok", token=mo._TOKEN_SUCCESS)

    async def _stage9(epics=None):
        return True, mo._log_stage(mo.STAGE_9_ALPHAS_ARMED, True, "ok", token=mo._TOKEN_SUCCESS)

    monkeypatch.setattr(mo, "_stage2_guardian_wake", _stage2)
    monkeypatch.setattr(mo, "_stage3_regime_hydration", _stage3)
    monkeypatch.setattr(mo, "_stage4_tuner_prime", _stage4)
    monkeypatch.setattr(mo, "_stage5_launch_core", _stage5)
    monkeypatch.setattr(mo, "_stage6_rest_auth", _stage6)
    monkeypatch.setattr(mo, "_stage7_stream_handshake", _stage7)
    monkeypatch.setattr(mo, "_stage8_data_feed_hydration", _stage8)
    monkeypatch.setattr(mo, "_stage9_alphas_armed", _stage9)
    import asyncio

    result = asyncio.run(mo._execute_warmup_async())
    assert result["ok"] is True
    assert calls["n"] >= 2


def test_route_applies_tuner_overlay(monkeypatch):
    snap = MagicMock()
    snap.state = 0
    snap.confidence = 0.8
    snap.strategy_gate = {"allow_entries": True, "size_factor": 0.85, "stop_factor": 0.9}

    def _merge(gate, state):
        gate["size_factor"] = 0.75

    monkeypatch.setattr("runtime.regime_switch_engine.evaluate_epic_regime", lambda e: snap)
    monkeypatch.setattr("runtime.parameter_tuner.merge_tuned_gate", _merge)
    route = mo.resolve_execution_route("CS.D.EURUSD.CFD.IP")
    assert route.size_factor_mult < 0.85


def test_dispatch_isolates_failing_epic(monkeypatch):
    def _resolve(epic):
        if epic == "BAD.EPIC.IP":
            raise TimeoutError("stream dropped")
        return mo.RouteDecision(
            epic=epic,
            regime_state=1,
            regime_label="hv_trend",
            execution_path="momentum_breakout",
            allow_entry=True,
            size_factor_mult=1.0,
            stop_factor_mult=1.0,
            kelly_fraction=0.1,
            confidence=0.7,
        )

    monkeypatch.setattr(mo, "resolve_execution_route", _resolve)
    import asyncio

    routes = asyncio.run(
        mo.dispatch_market_updates([("IX.D.DOW.IFM.IP", 0, 0), ("BAD.EPIC.IP", 0, 0)])
    )
    assert len(routes) == 1
    assert mo._epic_is_dropped("BAD.EPIC.IP") is True


def test_orchestrator_state_snapshot():
    mo._scoreboard.total_pp = 1050
    for stage in mo._BOOT_STAGES:
        mo._commit_stage_token(stage, mo._TOKEN_SUCCESS)
    mo._primed = True
    mo._boot_trade_ready = True
    mo._refresh_snapshot()
    mo.publish_iron_ledger_snapshot()
    snap = mo.get_orchestrator_state_snapshot()
    assert "scoreboard" in snap
    assert "strategy_matrix" in snap
    assert "warmup_logs" in snap
    assert "optimization" in snap
    assert "position_tree" in snap
    assert "last_ring_buffer_refresh_ts" in snap
    assert "stage_status" in snap
    assert "stage_tokens" in snap
    assert snap["scoreboard"]["total_pp"] == 1050


def test_orchestrator_light_fallback_exposes_boot_progress():
    from system.chaos_guardian import IronLedgerSnapshot

    mo.reset_master_orchestrator_for_tests()
    IronLedgerSnapshot.commit({"ts": 0, "orchestrator": {}})
    mo._armed = True
    mo._commit_stage_token(mo.STAGE_1_CONFIG_SANITY, mo._TOKEN_SUCCESS)
    mo._stage_health[mo.STAGE_2_GUARDIAN_WAKE] = mo.RAG_RUNNING
    snap = mo.get_orchestrator_state_snapshot()
    assert snap.get("iron_ledger") == "warming_light_fallback"
    assert snap.get("stage_tokens", {}).get(mo.STAGE_1_CONFIG_SANITY) == mo._TOKEN_SUCCESS
    assert snap.get("stage_status", {}).get(mo.STAGE_2_GUARDIAN_WAKE) == mo.RAG_RUNNING
    assert snap.get("ts", 0) > 0


def test_route_regime0_limit_chase(monkeypatch):
    snap = MagicMock()
    snap.state = 0
    snap.confidence = 0.8
    snap.strategy_gate = {"allow_entries": True, "size_factor": 0.85, "stop_factor": 0.9}
    monkeypatch.setattr("runtime.regime_switch_engine.evaluate_epic_regime", lambda e: snap)
    monkeypatch.setattr("runtime.dual_core_execution.epic_in_stagnant_dead_zone", lambda e: False)
    monkeypatch.setattr(mo, "validate_regime_entropy_arbitration", lambda e: (True, ""))
    route = mo.resolve_execution_route("CS.D.EURUSD.CFD.IP")
    assert route.execution_path == "limit_chase_hf"
    assert route.allow_entry is True
    assert route.regime_state == 0


def test_route_regime1_momentum(monkeypatch):
    snap = MagicMock()
    snap.state = 1
    snap.confidence = 0.85
    snap.strategy_gate = {"allow_entries": True, "size_factor": 1.1, "stop_factor": 1.25}
    monkeypatch.setattr("runtime.regime_switch_engine.evaluate_epic_regime", lambda e: snap)
    route = mo.resolve_execution_route("IX.D.DOW.IFM.IP")
    assert route.execution_path == "momentum_breakout"
    assert route.kelly_fraction <= mo._KELLY_MAX


def test_route_regime2_frozen(monkeypatch):
    snap = MagicMock()
    snap.state = 2
    snap.confidence = 0.6
    snap.strategy_gate = {"allow_entries": False, "stop_factor": 0.75}
    monkeypatch.setattr("runtime.regime_switch_engine.evaluate_epic_regime", lambda e: snap)
    route = mo.resolve_execution_route("IX.D.NIKKEI.IFM.IP")
    assert route.execution_path == "frozen"
    assert route.allow_entry is False
    mo._primed = True
    allowed, _ = mo.route_allows_entry("IX.D.NIKKEI.IFM.IP")
    assert allowed is False


def test_dispatch_market_updates_concurrent(monkeypatch):
    snap = MagicMock()
    snap.state = 1
    snap.confidence = 0.7
    snap.strategy_gate = {"allow_entries": True, "size_factor": 1.0, "stop_factor": 1.0}
    monkeypatch.setattr("runtime.regime_switch_engine.evaluate_epic_regime", lambda e: snap)
    monkeypatch.setattr("runtime.dual_core_execution.epic_in_stagnant_dead_zone", lambda e: False)
    monkeypatch.setattr(mo, "validate_regime_entropy_arbitration", lambda e: (True, ""))
    import asyncio

    routes = asyncio.run(
        mo.dispatch_market_updates(
            [("IX.D.DOW.IFM.IP", 100.0, 100.5), ("CS.D.CFPGOLD.CFP.IP", 2000.0, 2000.5)]
        )
    )
    assert len(routes) == 2
    assert all(r["execution_path"] == "momentum_breakout" for r in routes)


def test_get_scoreboard_multipliers_wired():
    mo.get_platform_scoreboard().total_pp = 1300
    assert mo.get_scoreboard_capacity_multiplier() > 1.0
    assert mo.get_scoreboard_size_multiplier() > 1.0
