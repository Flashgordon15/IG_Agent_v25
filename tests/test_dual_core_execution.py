"""Dual-core execution — volatility Z-score mode routing + micro scalp signals."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from runtime.dual_core_execution import (
    ENGINE_B_MICRO_SCALPER,
    MACRO_Z_THRESHOLD,
    MICRO_Z_THRESHOLD,
    MODE_MACRO,
    MODE_MICRO,
    MODE_NEUTRAL,
    PRIMARY_STACKED_EPIC,
    SECONDARY_STACKED_EPIC,
    canary_lot_size,
    evaluate_micro_scalp_signal,
    get_stacked_snapshots,
    ingest_hub_mid,
    reset_dual_core_for_tests,
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
