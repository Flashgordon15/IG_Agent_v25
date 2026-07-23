"""GBP exit and broker fill level tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import time

from execution.broker_fill_level import resolve_broker_fill_level
from runtime.micro_gbp_exit import (
    on_watchdog_tick,
    register_gbp_exit,
    reset_micro_gbp_exit_for_tests,
    snapshot,
    start_micro_gbp_exit_engine,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_micro_gbp_exit_for_tests()
    yield
    reset_micro_gbp_exit_for_tests()


def test_resolve_broker_fill_level_prefers_confirm():
    confirm = {"raw": {"level": 52878.6, "dealStatus": "ACCEPTED"}}
    assert resolve_broker_fill_level(confirm, hub_mid=100.14) == 52878.6


def test_gbp_exit_soft_loss_cuts_early():
    rest = MagicMock()
    start_micro_gbp_exit_engine(rest)
    register_gbp_exit(
        deal_id="D0",
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        size=10.0,
        entry_level=3340.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=8.0,
        trail_trigger_gbp=1.0,
        trail_lock_ratio=0.78,
        min_bank_win_gbp=0.75,
        max_giveback_ratio=0.28,
    )
    with patch(
        "runtime.micro_gbp_exit._load_broker_pnls_gbp",
        return_value={"D0": -2.3},
    ):
        with patch("runtime.micro_gbp_exit._POLL_MIN_SEC", 0.0):
            on_watchdog_tick()
    rest.close_position.assert_called_once()


def test_gbp_exit_cuts_loss_before_broker_twenty():
    rest = MagicMock()
    start_micro_gbp_exit_engine(rest)
    register_gbp_exit(
        deal_id="D1",
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        size=10.0,
        entry_level=3340.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=1.5,
        trail_lock_ratio=0.65,
        min_bank_win_gbp=1.0,
    )
    with patch(
        "runtime.micro_gbp_exit._load_broker_pnls_gbp",
        return_value={"D1": -5.5},
    ):
        with patch("runtime.micro_gbp_exit._POLL_MIN_SEC", 0.0):
            on_watchdog_tick()
            on_watchdog_tick()
    rest.close_position.assert_called_once()


def test_gbp_exit_trails_profit():
    rest = MagicMock()
    start_micro_gbp_exit_engine(rest)
    register_gbp_exit(
        deal_id="D2",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52800.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=4.0,
        trail_lock_ratio=0.70,
        min_bank_win_gbp=1.0,
    )
    pnls = iter([{"D2": 8.0}, {"D2": 8.0}, {"D2": 5.0}, {"D2": 5.0}])

    def _next():
        return next(pnls, {"D2": 5.0})

    with patch("runtime.micro_gbp_exit._load_broker_pnls_gbp", side_effect=_next):
        with patch("runtime.micro_gbp_exit._POLL_MIN_SEC", 0.0):
            on_watchdog_tick()
            on_watchdog_tick()
            on_watchdog_tick()
            on_watchdog_tick()
    time.sleep(0.3)
    rest.close_position.assert_called()
    assert snapshot()["tracks"] == {}


def test_gbp_exit_preserves_peak_on_rearm():
    start_micro_gbp_exit_engine(MagicMock())
    register_gbp_exit(
        deal_id="D3",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52800.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=1.5,
        trail_lock_ratio=0.65,
        min_bank_win_gbp=1.0,
    )
    with patch(
        "runtime.micro_gbp_exit._load_broker_pnls_gbp",
        return_value={"D3": 4.0},
    ):
        with patch("runtime.micro_gbp_exit._POLL_MIN_SEC", 0.0):
            on_watchdog_tick()
            on_watchdog_tick()
    peak_before = snapshot()["tracks"]["D3"]["peak_profit_gbp"]
    assert peak_before >= 4.0
    register_gbp_exit(
        deal_id="D3",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=52800.0,
        loss_cap_gbp=5.0,
        target_profit_gbp=12.5,
        trail_trigger_gbp=1.5,
        trail_lock_ratio=0.65,
        min_bank_win_gbp=1.0,
    )
    assert snapshot()["tracks"]["D3"]["peak_profit_gbp"] == peak_before
