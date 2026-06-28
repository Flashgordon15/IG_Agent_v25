"""Dual-core execution — volatility Z-score mode routing + micro scalp signals."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runtime.dual_core_execution import (
    ENGINE_B_MICRO_SCALPER,
    FAILOVER_BOTTOM_EPIC,
    FAILOVER_FOREX_STACK,
    FAILOVER_STATE_FOREX_LOCKED,
    FAILOVER_TOP_EPIC,
    MACRO_Z_THRESHOLD,
    MICRO_Z_THRESHOLD,
    MODE_MACRO,
    MODE_MICRO,
    MODE_NEUTRAL,
    PRIMARY_STACKED_EPIC,
    SECONDARY_STACKED_EPIC,
    canary_lot_size,
    evaluate_failover_tick_health,
    evaluate_micro_scalp_signal,
    get_active_stack_epics,
    get_failover_state,
    get_stacked_snapshots,
    ingest_hub_mid,
    is_forex_failover_active,
    is_piercing_zone_z,
    lock_forex_rotation_session,
    reset_dual_core_for_tests,
    resolve_max_spread_pts,
)


@pytest.fixture(autouse=True)
def _clean_dual_core():
    reset_dual_core_for_tests()
    yield
    reset_dual_core_for_tests()


def _seed_compressed_channel(epic: str = PRIMARY_STACKED_EPIC) -> None:
    """Narrow channel → compressed volatility → micro mode."""
    for i in range(140):
        mid = 1.1000 + (0.00001 if i % 2 == 0 else -0.00001)
        ingest_hub_mid(epic, mid)


def _seed_expanded_channel(epic: str = "IX.D.DOW.IFM.IP") -> None:
    """Wide channel after calm baseline → macro mode."""
    for i in range(110):
        ingest_hub_mid(epic, 52000.0 + (i % 2) * 0.25)
    for i in range(30):
        ingest_hub_mid(epic, 52000.0 + i * 200.0)


def test_micro_mode_when_z_below_threshold():
    _seed_compressed_channel()
    from runtime.dual_core_execution import get_dual_core_snapshot

    snap = get_dual_core_snapshot()
    assert snap.volatility_z_score < MICRO_Z_THRESHOLD
    assert snap.execution_mode == MODE_MICRO
    assert snap.core_b_micro_active is True
    assert snap.engine_b_armed is True


def test_macro_mode_when_z_above_threshold():
    _seed_expanded_channel()
    from runtime.dual_core_execution import get_dual_core_snapshot

    snap = get_dual_core_snapshot()
    assert snap.volatility_z_score >= MACRO_Z_THRESHOLD
    assert snap.execution_mode == MODE_MACRO
    assert snap.core_a_macro_active is True


def test_evaluate_micro_scalp_sell_at_upper_band():
    from runtime.dual_core_execution import get_stacked_snapshots

    _seed_compressed_channel(SECONDARY_STACKED_EPIC)
    snap = get_stacked_snapshots()[SECONDARY_STACKED_EPIC]
    assert snap.core_b_micro_active
    direction = evaluate_micro_scalp_signal(
        epic=snap.epic,
        bid=snap.micro_channel_upper,
        offer=snap.micro_channel_upper + 0.1,
        snap=snap,
    )
    assert direction == "SELL"


def test_canary_lot_size_fx_vs_index():
    cfg = MagicMock()
    cfg.max_deal_size_fx = 1.0
    cfg.max_deal_size_index = 0.5
    assert canary_lot_size("CS.D.EURUSD.CFD.IP", cfg) == 1.0
    assert canary_lot_size("IX.D.DOW.IFM.IP", cfg) == 0.5
    assert canary_lot_size("CS.D.CFPGOLD.CFP.IP", cfg) == 1.0


def test_dual_core_status_dict_shape():
    _seed_compressed_channel()
    from runtime.dual_core_execution import dual_core_status_dict

    payload = dual_core_status_dict()
    assert payload["execution_mode"] == MODE_MICRO
    assert payload["dual_core"]["core_b"]["engine"] == ENGINE_B_MICRO_SCALPER


def test_resolve_micro_stop_floors_to_broker_min():
    from runtime.dual_core_execution import resolve_micro_stop_limit_points

    with patch("execution.live_broker_order_router.floor_stop_distance_points") as floor:
        floor.side_effect = lambda _r, _e, pts: MagicMock(effective_points=max(pts, 6.0))
        tp, sl = resolve_micro_stop_limit_points(None, "IX.D.DOW.IFM.IP")
    assert tp >= 1.5
    assert sl >= 2.0


def test_resolve_max_spread_pts_defaults():
    assert resolve_max_spread_pts(FAILOVER_TOP_EPIC) == 3.0
    assert resolve_max_spread_pts(FAILOVER_BOTTOM_EPIC) == 4.0
    assert resolve_max_spread_pts(PRIMARY_STACKED_EPIC) == 12.0


def test_piercing_zone_accepts_deep_negative_z():
    """Z -3.4851 must pierce lower zone (<= -2.00) — not treated as out-of-bounds."""
    assert is_piercing_zone_z(-3.4851) is True
    assert is_piercing_zone_z(-2.0) is True
    assert is_piercing_zone_z(-2.5) is True
    assert is_piercing_zone_z(0.0) is True
    assert is_piercing_zone_z(2.5) is True


def test_forex_rotation_lock_sets_active_stack():
    state = lock_forex_rotation_session()
    assert is_forex_failover_active()
    assert get_active_stack_epics() == FAILOVER_FOREX_STACK
    assert state["forex_rotation_locked"] is True


def test_failover_activates_on_low_tick_velocity():
    snap = MagicMock()
    snap.bid = 1.0850
    snap.offer = 1.0852
    snap.age_seconds = MagicMock(return_value=1.0)
    hub = MagicMock()
    hub.get_snapshot.return_value = snap

    with patch("runtime.dual_core_execution.get_market_data_hub", return_value=hub):
        state = evaluate_failover_tick_health()

    assert state["health_breaches"]
    rot = state.get("last_rotation_reason") or ""
    assert get_active_stack_epics() != () or rot


def test_failover_activates_on_spread_breach():
    snap = MagicMock()
    snap.bid = 52000.0
    snap.offer = 52100.0  # 100pt spread >> 12pt DOW limit
    snap.age_seconds = MagicMock(return_value=1.0)
    hub = MagicMock()
    hub.get_snapshot.return_value = snap

    with patch("runtime.dual_core_execution.get_market_data_hub", return_value=hub):
        state = evaluate_failover_tick_health()

    assert state["health_breaches"]


def test_stagnant_dead_zone_triggers_rotation():
    import time as time_mod

    from runtime.dual_core_execution import (
        STAGNANT_DEAD_ZONE_REASON,
        STAGNANT_DEAD_ZONE_SEC,
        _rotate_active_stack_to,
        _stagnant_since_by_epic,
        evaluate_multi_source_rotation_sweep,
    )

    _rotate_active_stack_to(
        ("CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP"),
        reason="test_setup",
    )
    for i in range(140):
        ingest_hub_mid("IX.D.DOW.IFM.IP", 52000.0 + i * 5.0)
    _stagnant_since_by_epic["CS.D.EURUSD.CFD.IP"] = (
        time_mod.time() - STAGNANT_DEAD_ZONE_SEC - 5.0
    )
    with patch("runtime.dual_core_execution._fetch_multi_source_quote") as fetch:
        fetch.return_value = (1.1, 1.1002, "mock")
        state = evaluate_multi_source_rotation_sweep()
    assert "CS.D.EURUSD.CFD.IP" in (state.get("stagnant_rotated") or [])
    assert state.get("last_rotation_reason") == STAGNANT_DEAD_ZONE_REASON


def test_ml_failover_sovereignty_writes_overrides():
    from trading.continuous_optimization_worker import (
        get_continuous_optimization_worker,
        reset_continuous_optimization_worker_for_tests,
    )

    reset_continuous_optimization_worker_for_tests()
    snap = MagicMock()
    snap.bid = 1.0850
    snap.offer = 1.0852
    hub = MagicMock()
    hub.get_snapshot.return_value = snap

    with patch("runtime.dual_core_execution.get_market_data_hub", return_value=hub):
        evaluate_failover_tick_health()

    lock_forex_rotation_session(reason="test_ml_sovereignty")
    assert is_forex_failover_active()
    worker = get_continuous_optimization_worker()
    overrides = worker.run_failover_sovereignty(
        epic=FAILOVER_TOP_EPIC,
        spread=0.0002,
        slippage_pts=0.0001,
        latency_ms=42.0,
    )
    assert overrides.get("failover_forex_locked") is True
    assert overrides.get("target_lot") == 1.0
    assert "micro_z_threshold" in overrides
    assert worker.is_sovereignty_active()
    reset_continuous_optimization_worker_for_tests()


def test_forex_rotation_locked_disables_auto_rotation():
    from runtime.dual_core_execution import (
        epic_allowed_on_hot_path,
        lock_forex_rotation_session,
        multi_source_auto_rotation_enabled,
    )

    cfg = {"dual_core": {"forex_rotation_locked": True}}
    assert multi_source_auto_rotation_enabled(cfg) is False
    lock_forex_rotation_session(reason="test")
    assert multi_source_auto_rotation_enabled() is False
    assert epic_allowed_on_hot_path("CS.D.EURUSD.CFD.IP", cfg) is True
    assert epic_allowed_on_hot_path("IX.D.DOW.IFM.IP", cfg) is False
