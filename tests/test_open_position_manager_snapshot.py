"""OpenPositionManager broker-snapshot fast path tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from runtime.open_position_manager import (
    _fetch_open_rows,
    reset_open_position_manager_for_tests,
)


def setup_function() -> None:
    reset_open_position_manager_for_tests()


def teardown_function() -> None:
    reset_open_position_manager_for_tests()


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        position_management={"manager_enabled": True, "require_full_risk_stack": True},
    )


@patch("runtime.open_position_manager._gbp_tracks", return_value={})
@patch("runtime.broker_snapshot.read_snapshot")
def test_fetch_open_rows_prefers_broker_snapshot(mock_read, _mock_gbp) -> None:
    mock_read.return_value = {
        "source": "trade_support",
        "positions": [
            {
                "deal_id": "D_SNAP",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 52000.0,
                "pnl_gbp": -0.5,
            }
        ],
    }
    rest = MagicMock()
    rows, source, _age = _fetch_open_rows(rest, _cfg())
    assert len(rows) == 1
    assert rows[0].deal_id == "D_SNAP"
    assert "broker_snapshot" in source
    rest.open_positions.assert_not_called()
