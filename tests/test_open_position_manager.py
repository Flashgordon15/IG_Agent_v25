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


@patch("runtime.open_position_manager.execute_actions_bulk")
@patch("execution.position_risk_stack.reconcile_open_positions_risk_stack")
@patch("execution.position_risk_stack.ensure_risk_stack_coverage")
@patch("runtime.open_position_manager._gbp_tracks", return_value={})
@patch("runtime.open_position_manager.assess_open_positions")
@patch("runtime.open_position_manager._fetch_open_rows")
@patch("runtime.open_position_manager._ensure_sub_engines")
def test_unmonitored_grace_fail_closed_flatten(
    _mock_engines,
    mock_fetch,
    mock_assess,
    _mock_gbp,
    mock_ensure,
    mock_reconcile,
    mock_execute,
):
    """Still unmonitored after grace → flatten (APP RISK_STACK_DID_NOT_CUT fix)."""
    import runtime.open_position_manager as opm

    row = OpenPositionRow(
        deal_id="STILL_UNARMED",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry=53000.0,
        pnl_gbp=-1.2,
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
    mock_reconcile.return_value = {"armed": 0, "gbp": 0, "pruned": 0}

    cfg = _cfg()
    cfg.position_management = {
        **dict(cfg.position_management or {}),
        "unmonitored_grace_sec": 1.0,
        "flatten_unmonitored_after_grace": True,
    }

    # First tick: mark unmonitored_since, no flatten yet
    rest = MagicMock()
    r1 = run_management_tick(rest, cfg, execute=True)
    assert r1["unmonitored"] == 1
    assert r1.get("unmonitored_grace_flattens", 0) == 0

    # Age the tracker past grace
    opm._unmonitored_since["STILL_UNARMED"] = time.time() - 5.0
    r2 = run_management_tick(rest, cfg, execute=True)
    assert r2.get("unmonitored_grace_flattens", 0) == 1
    assert any("unmonitored_grace_flatten" in i for i in r2["issues"])
    assert mock_execute.called
    grace_report = mock_execute.call_args.args[1]
    assert any(
        a.action == "flatten" and "unmonitored_grace_exceeded" in a.reason
        for a in grace_report.actions
    )


def test_no_rest_client_returns_error():
    result = run_management_tick(None, _cfg(), execute=False)
    assert result["ok"] is False
    assert result["error"] == "no_rest_client"


def _timeout_cfg(*, timeout_sec: float = 0.15, stale_sec: float = 1.0):
    cfg = _cfg()
    cfg.position_management = {
        **dict(cfg.position_management or {}),
        "manager_tick_timeout_sec": timeout_sec,
        "manager_tick_stale_sec": stale_sec,
    }
    return cfg


def _hanging_impl(**_kwargs):
    time.sleep(30)
    return {"ok": True}


@patch("runtime.open_position_manager._tick_timeout_sec", return_value=0.15)
@patch("runtime.open_position_manager._broker_book_is_flat", return_value=True)
@patch("runtime.open_position_manager._attach_broker_stops_on_timeout")
@patch("runtime.open_position_manager._run_management_tick_impl")
def test_tick_timeout_flat_book_soft_ok_clears_sticky_error(
    mock_impl, mock_stops, _mock_flat, _mock_timeout
):
    """Hung tick + flat book → soft-ok; must not leave last_error=tick_timeout."""
    from runtime.open_position_manager import snapshot

    mock_impl.side_effect = _hanging_impl
    rest = MagicMock()
    result = run_management_tick(rest, _timeout_cfg(), execute=True)

    assert result["ok"] is True
    assert result.get("timed_out") is True
    assert result.get("flat_book_soft_ok") is True
    assert result.get("note") == "tick_timeout_ignored_flat_book"
    assert result.get("error") == ""
    assert "broker_stop_fallback" not in result
    mock_stops.assert_not_called()

    snap = snapshot()
    assert snap["tick_count"] >= 1
    assert snap["last_error"] == ""
    assert snap["last_report"].get("flat_book_soft_ok") is True
    assert snap["last_report"].get("timed_out") is True


@patch("runtime.open_position_manager._tick_timeout_sec", return_value=0.15)
@patch("runtime.open_position_manager._broker_book_is_flat", return_value=False)
@patch("runtime.open_position_manager._attach_broker_stops_on_timeout")
@patch("runtime.open_position_manager._run_management_tick_impl")
def test_tick_timeout_open_book_hard_fail_attaches_broker_stops(
    mock_impl, mock_stops, _mock_flat, _mock_timeout
):
    """Hung tick with open risk → hard tick_timeout + broker-stop fallback.

    This is the sticky-REST path: last_error must remain tick_timeout so
    liveness/desk_support can recover while opens are live.
    """
    from runtime.open_position_manager import snapshot

    mock_impl.side_effect = _hanging_impl
    mock_stops.return_value = {"ok": True, "armed": 1, "error": ""}
    rest = MagicMock()
    result = run_management_tick(rest, _timeout_cfg(), execute=True)

    assert result["ok"] is False
    assert result.get("error") == "tick_timeout"
    assert result.get("timed_out") is True
    assert result.get("flat_book_soft_ok") is not True
    assert result.get("broker_stop_fallback") == {"ok": True, "armed": 1, "error": ""}
    mock_stops.assert_called_once()
    assert mock_stops.call_args.args[0] is rest

    snap = snapshot()
    assert snap["tick_count"] >= 1
    assert snap["last_error"] == "tick_timeout"
    assert snap["last_report"].get("error") == "tick_timeout"


@patch("runtime.open_position_manager._run_management_tick_impl")
def test_stale_in_flight_allows_new_tick(mock_impl):
    import runtime.open_position_manager as opm

    call_count = {"n": 0}

    def _slow(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            time.sleep(0.05)
        return {"ok": True, "broker_open": 0}

    mock_impl.side_effect = _slow
    cfg = _timeout_cfg(timeout_sec=2.0, stale_sec=0.01)
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


def test_ensure_open_position_manager_rearms_dead_daemon():
    from runtime.open_position_manager import (
        ensure_open_position_manager,
        reset_open_position_manager_for_tests,
        snapshot,
        stop_open_position_manager,
    )

    reset_open_position_manager_for_tests()
    assert snapshot().get("active") is False

    cfg = SimpleNamespace(
        position_management={
            "manager_enabled": True,
            "manager_poll_sec": 0.2,
            "manager_tick_timeout_sec": 1.0,
        },
    )
    with patch("runtime.open_position_manager._first_tick"), \
         patch("runtime.open_position_manager.run_management_tick", return_value={"ok": True}):
        out = ensure_open_position_manager(MagicMock(), cfg=cfg)
        assert out.get("ok") is True
        assert out.get("active") is True
        assert snapshot().get("active") is True
        assert snapshot().get("thread_alive") is True

        out2 = ensure_open_position_manager(MagicMock(), cfg=cfg)
        assert out2.get("ok") is True
        assert out2.get("rearmed") is False

        stop_open_position_manager()
        time.sleep(0.3)
        assert snapshot().get("active") is False
    reset_open_position_manager_for_tests()
