"""Tests for out-of-process Desk Support Wrapper."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runtime.desk_support_wrapper import (
    DeskSupportState,
    DeskSupportWrapper,
    MonitorSnapshot,
    dual_protected_listener_pids,
    epic_cap_breach,
    is_zombie,
    process_state,
    stale_lock_diagnosis,
    v32_dual_port_active,
)


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    monkeypatch.setenv("APP_MODE", "DEMO")
    cfg = {
        "desk_support_wrapper": {
            "enabled": True,
            "poll_interval_sec": 15,
            "max_restarts_per_hour": 3,
            "restart_cooldown_sec": 120,
            "api_port": 8080,
            "trade_ready_timeout_sec": 180,
            "manager_tick_stale_sec": 60,
        }
    }
    w = DeskSupportWrapper(cfg)
    w.state.started_mono = 0.0  # bypass startup grace in tests
    return w


def test_healthy_health_selects_no_action(wrapper):
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": True, "trading_loops_running": True},
        liveness={"ok": True},
        manager={"active": True, "tick_count": 5, "last_tick_at": time.time()},
        positions={"count": 0, "unmonitored": 0, "verdict": "FLAT"},
    )
    snap.problems = wrapper._diagnose(snap)
    assert wrapper.select_action(snap) is None


@patch("runtime.desk_support_wrapper._fetch_json")
@patch("runtime.desk_support_wrapper.port_bound", return_value=False)
@patch("runtime.desk_support_wrapper.list_main_py_pids", return_value=[])
@patch("runtime.desk_support_wrapper.list_session_ready_pids", return_value=[])
@patch("runtime.desk_support_wrapper.stale_lock_diagnosis")
def test_api_down_triggers_anti_zombie(
    mock_lock, mock_sr, mock_main, mock_port, mock_fetch, wrapper
):
    mock_lock.return_value = {"issues": ["session_lock_stale"], "lock_pid": 999}
    mock_fetch.return_value = (None, "URLError")
    wrapper.state.last_restart_mono = 0.0
    wrapper.state.api_down_since = time.time() - 60.0

    with patch.object(wrapper, "anti_zombie_recovery", return_value={"started": True}) as mock_rec:
        result = wrapper.poll_once()

    assert result["action"] == "anti_zombie_recovery"
    mock_rec.assert_called_once()


@patch("runtime.desk_support_wrapper.process_age_sec", return_value=45.0)
@patch("runtime.desk_support_wrapper.is_zombie", return_value=False)
@patch("runtime.desk_support_wrapper.pid_alive", return_value=True)
@patch("runtime.desk_support_wrapper._fetch_json")
@patch("runtime.desk_support_wrapper.port_bound", return_value=False)
@patch("runtime.desk_support_wrapper.list_main_py_pids", return_value=[4242])
@patch("runtime.desk_support_wrapper.list_session_ready_pids", return_value=[])
@patch("runtime.desk_support_wrapper.stale_lock_diagnosis")
def test_young_live_main_skips_anti_zombie_during_boot(
    mock_lock, mock_sr, mock_main, mock_port, mock_fetch, mock_alive, mock_zom, mock_age, wrapper
):
    """Gate-2 hydrate can unbind :8080 briefly — do not SIGTERM a young live main."""
    mock_lock.return_value = {"issues": [], "lock_pid": 4242}
    mock_fetch.return_value = (None, "URLError")
    wrapper.state.last_restart_mono = 0.0
    wrapper.state.api_down_since = time.time() - 60.0

    with patch.object(wrapper, "anti_zombie_recovery") as mock_rec:
        result = wrapper.poll_once()

    assert result.get("action") != "anti_zombie_recovery"
    mock_rec.assert_not_called()


def test_cooldown_prevents_restart_storm(wrapper):
    snap = MonitorSnapshot(
        api_up=False,
        port_bound=False,
        problems=["api_port_down"],
    )
    wrapper.state.last_restart_mono = time.monotonic()
    wrapper.cfg["restart_cooldown_sec"] = 600

    out = wrapper.execute_action("anti_zombie_recovery", snap)
    assert out.get("skipped") is True
    assert out.get("reason") == "cooldown"


def test_restart_cap_escalates(wrapper):
    snap = MonitorSnapshot(
        api_up=False,
        port_bound=False,
        problems=["api_port_down"],
    )
    wrapper.state.restart_times = [time.time()] * 3
    wrapper.state.last_restart_mono = 0.0

    with patch.object(wrapper, "anti_zombie_recovery") as mock_rec:
        wrapper.execute_action("anti_zombie_recovery", snap)

    assert wrapper.state.escalated is True
    mock_rec.assert_not_called()


@patch("runtime.session_lock.pid_alive", return_value=False)
@patch("runtime.session_lock.session_is_healthy", return_value=False)
@patch("runtime.session_lock.read_session_lock")
@patch("runtime.session_lock.lock_path_for_scope")
@patch("runtime.session_lock.resolve_account_scope", return_value="ig:test")
@patch("runtime.app_mode.resolve_data_root", return_value="/tmp/data")
@patch("runtime.app_mode.resolve_app_mode")
def test_stale_lock_detection(
    mock_mode,
    mock_root,
    mock_scope,
    mock_lpath,
    mock_read,
    mock_healthy,
    mock_alive,
):
    mock_mode.return_value = MagicMock()
    path = Path("/tmp/data/session_ig_test.lock")
    mock_lpath.return_value = path
    mock_read.return_value = {"pid": 4242, "port": 8080}

    with patch("system.identity.instance_lock.read_lock_holder", return_value=None):
        diag = stale_lock_diagnosis(port=8080, data_root=Path("/tmp/data"))

    assert "session_lock_stale" in diag["issues"]
    assert diag["lock_pid"] == 4242


@patch("runtime.desk_support_wrapper.subprocess.run")
def test_zombie_detection(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="Z\n")
    assert process_state(1234) == "Z"
    assert is_zombie(1234) is True

    mock_run.return_value = MagicMock(returncode=0, stdout="S\n")
    assert is_zombie(1234) is False


def test_unmonitored_positions_trigger_recover(wrapper):
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": True, "trading_loops_running": True},
        positions={"count": 2, "unmonitored": 1, "verdict": "DEGRADED"},
        manager={"active": True, "tick_count": 0, "last_tick_at": 0},
    )
    snap.problems = wrapper._diagnose(snap)
    action = wrapper.select_action(snap)
    assert action == "recover_and_supervise"


def test_flat_false_stale_skips_recover_and_supervise(wrapper):
    """Flat broker book + stale cache must not drive recover_and_supervise."""
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": True, "trading_loops_running": True},
        liveness={
            "ok": False,
            "has_open_risk": False,
            "open_count": 0,
            "issues": ["positions_snapshot_stale", "ig_sync_missing"],
        },
        manager={"active": True, "tick_count": 5, "last_tick_at": time.time()},
        positions={
            "count": 0,
            "unmonitored": 0,
            "verdict": "FLAT",
            "stale": True,
            "trade_support": {"broker_open": 0},
            "broker_open_sot": {"count": 0, "source": "trade_support"},
        },
    )
    snap.problems = wrapper._diagnose(snap)
    assert "positions_stale" not in snap.problems
    assert "liveness_degraded" not in snap.problems
    assert wrapper.select_action(snap) is None


def test_flat_liveness_noise_in_problems_still_skipped(wrapper):
    """Defense in depth: even if problems list has soft noise, stay idle when flat."""
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": True, "trading_loops_running": True},
        positions={"count": 0, "unmonitored": 0, "verdict": "FLAT", "stale": True},
        problems=["liveness_degraded", "positions_stale"],
    )
    assert wrapper.select_action(snap) is None


def test_trade_ready_false_waits_then_recovers(wrapper):
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": False, "trading_loops_running": True},
    )
    snap.problems = wrapper._diagnose(snap)
    assert wrapper.select_action(snap) is None

    wrapper.state.trade_ready_false_since = time.time() - 200
    assert wrapper.select_action(snap) == "post_recover"


def test_epic_cap_breach():
    positions = {
        "positions": [
            {"epic": "IX.D.DOW.IFM.IP"},
            {"epic": "IX.D.DOW.IFM.IP"},
            {"epic": "IX.D.DOW.IFM.IP"},
        ]
    }
    breached, detail = epic_cap_breach(positions, max_per_epic=2)
    assert breached is True
    assert detail["IX.D.DOW.IFM.IP"] == 3


@patch("runtime.desk_support_wrapper._fetch_json")
def test_mock_health_degraded_liveness(mock_fetch, wrapper):
    mock_fetch.side_effect = [
        ({"ok": True, "trade_ready": True, "trading_loops_running": True}, None),
        ({"ok": False, "issues": ["ig_sync_stale"]}, None),
        ({"active": True, "tick_count": 2, "last_tick_at": time.time()}, None),
        ({"count": 1, "unmonitored": 0, "verdict": "HEALTHY"}, None),
    ]
    with patch("runtime.desk_support_wrapper.port_bound", return_value=True):
        with patch("runtime.desk_support_wrapper.list_main_py_pids", return_value=[100]):
            with patch("runtime.desk_support_wrapper.list_session_ready_pids", return_value=[]):
                with patch(
                    "runtime.desk_support_wrapper.stale_lock_diagnosis",
                    return_value={"issues": []},
                ):
                    snap = wrapper.collect_snapshot()
    assert "liveness_degraded" in snap.problems


def test_stuck_session_ready_kill(wrapper):
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        session_ready_pids=[100, 200, 300],
        problems=["stuck_session_ready:[100, 200, 300]"],
    )
    with patch.object(
        wrapper, "_terminate_pids", return_value=[100, 200]
    ) as mock_term:
        out = wrapper.execute_action("kill_stuck_session_ready", snap)
    mock_term.assert_called_once_with([100, 200], label="stuck_session_ready")
    assert out["killed"] == [100, 200]


def test_hung_boot_detected_and_reaped(wrapper):
    """A session_ready alive past boot_hang_sec with port down = wedged boot."""
    wrapper.cfg["boot_hang_sec"] = 240.0
    wrapper.state.session_ready_first_seen = {500: time.monotonic() - 300.0}
    snap = MonitorSnapshot(
        api_up=False,
        port_bound=False,
        session_ready_pids=[500],
        main_pids=[],
    )
    with patch("runtime.desk_support_wrapper._child_pids", return_value=[]):
        snap.problems = wrapper._diagnose(snap)
    assert any(p.startswith("hung_boot:") for p in snap.problems)
    assert wrapper.select_action(snap) == "reap_hung_boot"


def test_hung_boot_zombie_child_detected(wrapper):
    """A defunct main.py child under a live session_ready = dead boot to reap."""
    snap = MonitorSnapshot(
        api_up=False,
        port_bound=False,
        session_ready_pids=[500],
        main_pids=[],
    )
    with patch("runtime.desk_support_wrapper._child_pids", return_value=[777]):
        with patch("runtime.desk_support_wrapper.is_zombie", return_value=True):
            snap.problems = wrapper._diagnose(snap)
    assert any(p.startswith("hung_boot_zombie_child:") for p in snap.problems)
    assert wrapper.select_action(snap) == "reap_hung_boot"


def test_reap_hung_boot_kills_tree_and_restarts(wrapper):
    snap = MonitorSnapshot(
        api_up=False, port_bound=False, problems=["hung_boot:[500]"]
    )
    wrapper.state.last_restart_mono = 0.0
    with patch(
        "runtime.desk_support_wrapper.list_session_ready_pids", return_value=[500]
    ), patch(
        "runtime.desk_support_wrapper.process_tree_pids", return_value=[500, 777]
    ), patch(
        "runtime.desk_support_wrapper.list_main_py_pids", return_value=[]
    ), patch(
        "runtime.desk_support_wrapper.stale_lock_diagnosis",
        return_value={"lock_pid": None},
    ), patch.object(
        wrapper, "_terminate_pids", return_value=[500, 777]
    ) as mock_term, patch.object(
        wrapper, "_clear_locks", return_value=["session"]
    ), patch.object(
        wrapper, "_start_agent", return_value=True
    ) as mock_start:
        out = wrapper.execute_action("reap_hung_boot", snap)
    mock_term.assert_called_once()
    mock_start.assert_called_once()
    assert out["started"] is True
    assert 500 in mock_term.call_args[0][0] and 777 in mock_term.call_args[0][0]


def test_escalation_is_timed_backoff_not_permanent(wrapper):
    """After the restart cap, escalate() backs off then resumes recovery."""
    wrapper.cfg["escalation_backoff_sec"] = 600.0
    with patch.object(wrapper, "_send_alert", return_value=True) as mock_alert:
        wrapper.escalate(reason="cap hit")
    assert wrapper.state.escalated is True
    assert wrapper.state.escalated_until_mono > time.monotonic()
    mock_alert.assert_called_once()

    # Within backoff window → no action.
    snap = MonitorSnapshot(api_up=False, port_bound=False, problems=["api_port_down"])
    assert wrapper.select_action(snap) is None

    # Window elapsed → escalation clears and recovery resumes.
    wrapper.state.escalated_until_mono = time.monotonic() - 1.0
    wrapper.state.api_down_since = time.time() - 60.0
    action = wrapper.select_action(snap)
    assert wrapper.state.escalated is False
    assert action == "anti_zombie_recovery"


def test_dead_mans_switch_alert_on_sustained_down(wrapper):
    wrapper.cfg["down_alert_sec"] = 300.0
    wrapper.state.api_down_since = time.time() - 400.0
    snap = MonitorSnapshot(api_up=False, port_bound=False, problems=["api_port_down"])
    with patch.object(wrapper, "_send_alert", return_value=True) as mock_alert:
        wrapper.select_action(snap)
    assert wrapper.state.down_alert_sent is True
    assert any(
        c.kwargs.get("dedupe_key") == "desk_support_api_down"
        for c in mock_alert.call_args_list
    )


def test_dead_mans_switch_recovery_alert(wrapper):
    wrapper.state.down_alert_sent = True
    snap = MonitorSnapshot(
        api_up=True,
        port_bound=True,
        health={"ok": True, "trade_ready": True, "trading_loops_running": True},
    )
    snap.problems = wrapper._diagnose(snap)
    with patch.object(wrapper, "_send_alert", return_value=True) as mock_alert:
        wrapper.select_action(snap)
    assert wrapper.state.down_alert_sent is False
    assert any(
        c.kwargs.get("dedupe_key") == "desk_support_api_recovered"
        for c in mock_alert.call_args_list
    )


def test_audit_log_written(tmp_path, monkeypatch, wrapper):
    audit = tmp_path / "desk_support_audit.jsonl"
    monkeypatch.setattr("runtime.desk_support_wrapper._AUDIT_PATH", audit)
    from runtime.desk_support_wrapper import _audit

    _audit("test_event", {"foo": "bar"})
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "test_event"
    assert row["foo"] == "bar"


def test_anti_zombie_dual_protects_healthy_sibling(wrapper):
    """v32 twin on :8080 must survive recovery for a down :8081 sibling."""
    with (
        patch("runtime.desk_support_wrapper.v32_dual_port_active", return_value=True),
        patch("runtime.desk_support_wrapper.dual_watch_ports", return_value=[8080, 8081]),
        patch("runtime.desk_support_wrapper.dual_down_ports", return_value=[8081]),
        patch(
            "runtime.desk_support_wrapper.dual_protected_listener_pids",
            return_value={1000},
        ),
        patch("runtime.desk_support_wrapper.list_main_py_pids", return_value=[1000, 2000]),
        patch("runtime.desk_support_wrapper.list_session_ready_pids", return_value=[]),
        patch(
            "runtime.desk_support_wrapper.stale_lock_diagnosis",
            return_value={"lock_pid": None, "instance_holder": None},
        ),
        patch("runtime.desk_support_wrapper.port_listener_pid", return_value=None),
        patch.object(wrapper, "_clear_locks", return_value=[]),
        patch.object(wrapper, "_start_dual_heal", return_value=True),
        patch("system.shutdown_cleanup.mark_manual_stop"),
        patch("system.shutdown_cleanup.clear_manual_stop"),
        patch.object(wrapper, "_terminate_pids", return_value=[2000]) as mock_term,
    ):
        out = wrapper.anti_zombie_recovery(reason="api_port_down")

    assert out["dual_port"] is True
    assert out["protected_pids"] == [1000]
    mock_term.assert_called_once()
    targets = mock_term.call_args[0][0]
    assert 1000 not in targets
    assert 2000 in targets
    assert mock_term.call_args[1]["protected_pids"] == {1000}


def test_v32_dual_port_active_from_env(monkeypatch):
    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")
    assert v32_dual_port_active() is True


def test_dual_protected_listener_pids_skips_down_port(wrapper):
    with (
        patch("runtime.desk_support_wrapper.dual_watch_ports", return_value=[8080, 8081]),
        patch(
            "runtime.desk_support_wrapper.port_bound",
            side_effect=lambda p: p == 8080,
        ),
        patch(
            "runtime.desk_support_wrapper.port_listener_pid",
            side_effect=lambda p: 4242 if p == 8080 else None,
        ),
    ):
        protected = dual_protected_listener_pids()
    assert protected == {4242}
