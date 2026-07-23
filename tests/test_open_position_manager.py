"""Tests for in-process OpenPositionManager supervision."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from execution.open_position_rules import ManageReport, OpenPositionRow
from runtime.open_position_manager import (
    reset_open_position_manager_for_tests,
    run_management_tick,
)


def setup_function():
    reset_open_position_manager_for_tests()


def teardown_function():
    reset_open_position_manager_for_tests()


def _cfg():
    return SimpleNamespace(
        max_open_positions=6,
        max_positions_per_epic=2,
        position_management={"manager_enabled": True, "enforce_cap_breach": True},
    )


@patch("runtime.open_position_manager.execute_actions_bulk")
@patch("runtime.open_position_manager.assess_open_positions")
@patch("runtime.open_position_manager._fetch_open_rows")
@patch("runtime.open_position_manager._ensure_sub_engines")
def test_management_tick_returns_report(
    _mock_engines, mock_fetch, mock_assess, mock_execute
):
    row = OpenPositionRow(
        deal_id="D1",
        epic="IX.D.NIKKEI.IFM.IP",
        direction="BUY",
        size=0.5,
        entry=68000.0,
        pnl_gbp=1.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=1.68,
        target_gbp=10.0,
        trail_trigger_gbp=1.25,
    )
    mock_fetch.return_value = ([row], "sync_cache", 2.0)
    mock_assess.return_value = ManageReport(
        broker_open=1,
        assessed=1,
        positions=[],
        issues=[],
        actions=[],
    )

    rest = MagicMock()
    result = run_management_tick(rest, _cfg(), execute=True)

    assert result["ok"] is True
    assert result["broker_open"] == 1
    mock_execute.assert_not_called()


@patch("execution.position_risk_stack.reconcile_open_positions_risk_stack")
@patch("execution.position_risk_stack.ensure_risk_stack_coverage")
@patch("runtime.open_position_manager._gbp_tracks", return_value={})
@patch("runtime.open_position_manager.execute_actions_bulk")
@patch("runtime.open_position_manager.assess_open_positions")
@patch("runtime.open_position_manager._fetch_open_rows")
@patch("runtime.open_position_manager._ensure_sub_engines")
def test_unmonitored_escalates_risk_stack(
    _mock_engines,
    mock_fetch,
    mock_assess,
    _mock_execute,
    mock_gbp,
    mock_ensure,
    mock_reconcile,
):
    row = OpenPositionRow(
        deal_id="UNARMED",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry=53000.0,
        pnl_gbp=-0.5,
        loss_cap_gbp=4.0,
        soft_loss_gbp=1.68,
        target_gbp=10.0,
        trail_trigger_gbp=1.25,
    )
    mock_fetch.return_value = ([row], "rest", None)
    mock_assess.return_value = ManageReport(
        broker_open=1,
        assessed=1,
        positions=[],
        issues=[],
        actions=[],
    )
    mock_reconcile.return_value = {"armed": 1, "gbp": 1, "pruned": 0}

    rest = MagicMock()
    result = run_management_tick(rest, _cfg(), execute=True)

    assert result["unmonitored"] == 1
    mock_ensure.assert_called_once()
    mock_reconcile.assert_called_once()
    assert any("unmonitored_escalation" in i for i in result["issues"])


def test_no_rest_client_returns_error():
    result = run_management_tick(None, _cfg(), execute=False)
    assert result["ok"] is False
    assert result["error"] == "no_rest_client"


@patch("runtime.open_position_manager._run_management_tick_impl")
def test_tick_count_increments_on_timeout(mock_impl):
    from runtime.open_position_manager import snapshot

    def _hang(**_kwargs):
        import time

        time.sleep(30)
        return {"ok": True}

    mock_impl.side_effect = _hang
    cfg = _cfg()
    cfg.position_management = {
        **(_cfg().position_management or {}),
        "manager_tick_timeout_sec": 0.2,
        "manager_tick_stale_sec": 1.0,
    }
    rest = MagicMock()
    result = run_management_tick(rest, cfg, execute=True)
    assert result["ok"] is False
    assert result.get("error") == "tick_timeout"
    snap = snapshot()
    assert snap["tick_count"] >= 1
    assert snap["last_report"].get("error") == "tick_timeout"


@patch("runtime.open_position_manager._run_management_tick_impl")
def test_stale_in_flight_allows_new_tick(mock_impl):
    import runtime.open_position_manager as opm

    call_count = {"n": 0}

    def _slow(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            import time

            time.sleep(0.05)
        return {"ok": True, "broker_open": 0}

    mock_impl.side_effect = _slow
    cfg = _cfg()
    cfg.position_management = {
        **(_cfg().position_management or {}),
        "manager_tick_timeout_sec": 2.0,
        "manager_tick_stale_sec": 0.01,
    }
    rest = MagicMock()

    with opm._state_lock:
        opm._tick_running = True
        opm._tick_started_at = time.time() - 0.005

    skipped = run_management_tick(rest, cfg, execute=False)
    assert skipped.get("error") == "tick_in_progress"

    with opm._state_lock:
        opm._tick_running = True
        opm._tick_started_at = time.time() - 5.0

    recovered = run_management_tick(rest, cfg, execute=False)
    assert recovered.get("ok") is True


@patch("runtime.open_position_manager._run_management_tick_impl")
def test_overlapping_tick_skipped_without_deadlock(mock_impl):
    import threading

    mock_impl.return_value = {"ok": True, "broker_open": 1}
    rest = MagicMock()
    cfg = _cfg()
    barrier = threading.Barrier(2, timeout=2.0)

    def _first():
        barrier.wait()
        return run_management_tick(rest, cfg, execute=False)

    def _second():
        barrier.wait()
        return run_management_tick(rest, cfg, execute=False)

    t1 = threading.Thread(target=_first)
    t2 = threading.Thread(target=_second)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert not t1.is_alive()
    assert not t2.is_alive()
