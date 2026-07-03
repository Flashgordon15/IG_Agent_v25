"""Broker reconciliation daemon — drift streak before kill-switch trip."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_daemon():
    import system.broker_reconciliation_daemon as brd

    brd._drift_streak = 0
    yield
    brd._drift_streak = 0


def test_reconcile_requires_three_drift_ticks_before_kill_switch():
    from system.broker_reconciliation_daemon import run_reconciliation_once

    rest = MagicMock()
    rest.get_open_positions.return_value = [{"position": {"dealId": "A"}}] * 5

    with (
        patch(
            "system.broker_reconciliation_daemon._count_internal_positions",
            return_value=0,
        ),
        patch(
            "system.broker_reconciliation_daemon._reconcile_lifecycle_registry",
            return_value={},
        ),
        patch(
            "runtime.strategy_kill_switch.trip_master_strategy_kill_switch"
        ) as trip,
    ):
        for _ in range(2):
            snap = run_reconciliation_once(rest=rest)
            assert snap["healthy"] is False
            trip.assert_not_called()
        snap = run_reconciliation_once(rest=rest)
        assert snap["healthy"] is False
        trip.assert_called_once()


def test_reconcile_before_drift_clears_streak():
    from system.broker_reconciliation_daemon import run_reconciliation_once

    rest = MagicMock()
    rest.get_open_positions.return_value = [{"position": {"dealId": "A"}}] * 5

    with (
        patch(
            "system.broker_reconciliation_daemon._count_internal_positions",
            return_value=0,
        ),
        patch(
            "system.broker_reconciliation_daemon._reconcile_lifecycle_registry",
            return_value={},
        ),
        patch(
            "runtime.strategy_kill_switch.trip_master_strategy_kill_switch"
        ) as trip,
    ):
        run_reconciliation_once(rest=rest)
        rest.get_open_positions.return_value = []
        run_reconciliation_once(rest=rest)
        run_reconciliation_once(rest=rest)
        run_reconciliation_once(rest=rest)
        trip.assert_not_called()
