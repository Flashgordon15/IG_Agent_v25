"""MicroGbpExit broker snapshot P&L fallback tests."""

from __future__ import annotations

from unittest.mock import patch

from runtime.micro_gbp_exit import (
    _load_broker_pnls_gbp,
    register_gbp_exit,
    reset_micro_gbp_exit_for_tests,
)


def setup_function() -> None:
    reset_micro_gbp_exit_for_tests()


def teardown_function() -> None:
    reset_micro_gbp_exit_for_tests()


@patch("runtime.broker_snapshot.read_snapshot")
def test_load_broker_pnls_from_shared_snapshot(mock_read) -> None:
    mock_read.return_value = {
        "positions": [
            {
                "deal_id": "D1",
                "pnl_gbp": 2.5,
            }
        ]
    }
    register_gbp_exit(
        deal_id="D1",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52000.0,
        loss_cap_gbp=4.0,
        target_profit_gbp=8.0,
        trail_trigger_gbp=1.0,
    )
    pnls = _load_broker_pnls_gbp()
    assert pnls.get("D1") == 2.5
