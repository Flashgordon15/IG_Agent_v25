"""Tests for strategy improvement measurement."""

from __future__ import annotations

from runtime.strategy_improvement_tracker import (
    record_managed_close,
    reset_strategy_improvement_for_tests,
    snapshot,
)


def setup_function():
    reset_strategy_improvement_for_tests()


def teardown_function():
    reset_strategy_improvement_for_tests()


def test_record_and_snapshot_windows():
    record_managed_close(epic="IX.D.DOW.IFM.IP", pnl_gbp=2.5, exit_reason="micro_bank peak=3")
    record_managed_close(epic="IX.D.DOW.IFM.IP", pnl_gbp=-1.5, exit_reason="soft_loss")
    record_managed_close(epic="IX.D.NIKKEI.IFM.IP", pnl_gbp=1.2, exit_reason="quick_win")

    snap = snapshot()
    w20 = snap["windows"]["last_20"]
    assert w20["n"] == 3
    assert w20["wins"] == 2
    assert w20["win_rate"] == round(2 / 3, 4)
    assert "by_exit_reason" in snap
