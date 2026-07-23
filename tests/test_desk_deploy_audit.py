"""Tests for desk deploy audit helper."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.desk_deploy_audit import AuditReport, run_audit  # noqa: E402


@patch("scripts.desk_deploy_audit._http_get")
@patch("scripts.desk_deploy_audit._port_listening", return_value=True)
@patch("scripts.desk_deploy_audit._pgrep")
def test_audit_flat_allows_deploy(mock_pgrep, _mock_port, mock_http):
    mock_pgrep.side_effect = lambda pat: {
        "src/main.py": [(100, "python src/main.py")],
        "runtime.trade_support_wrapper": [(101, "trade_support")],
        "runtime.desk_support_wrapper": [(102, "desk_support")],
        "manage_live_positions.py --supervise": [],
    }.get(pat, [])

    mock_http.side_effect = lambda path, timeout=5.0: {
        "/api/positions/live": {"count": 0, "unmonitored": 0},
        "/api/position_manager/status": {
            "active": True,
            "tick_count": 5,
            "last_error": "",
            "last_report": {"ok": True},
        },
    }[path]

    with patch("scripts.desk_deploy_audit._pid_alive", return_value=True):
        with patch("scripts.desk_deploy_audit._wrapper_status", return_value={"present": True, "stale": False, "age_sec": 5.0}):
            with patch("scripts.desk_deploy_audit.manual_stop_active", create=True):
                with patch("system.shutdown_cleanup.manual_stop_active", return_value=False):
                    report = run_audit()

    assert isinstance(report, AuditReport)
    assert report.flat is True
    assert report.broker_open == 0
    assert report.deploy_allowed is True
    assert report.session_state == "deploy_window"


@patch("scripts.desk_deploy_audit._http_get")
@patch("scripts.desk_deploy_audit._port_listening", return_value=True)
@patch("scripts.desk_deploy_audit._pgrep")
def test_audit_open_positions_block_deploy(mock_pgrep, _mock_port, mock_http):
    mock_pgrep.return_value = [(100, "python src/main.py")]

    mock_http.side_effect = lambda path, timeout=5.0: {
        "/api/positions/live": {"count": 3, "unmonitored": 0},
        "/api/position_manager/status": {"active": True, "tick_count": 0},
    }[path]

    with patch("scripts.desk_deploy_audit._pid_alive", return_value=True):
        with patch("scripts.desk_deploy_audit._wrapper_status", return_value={"present": True, "stale": False}):
            with patch("system.shutdown_cleanup.manual_stop_active", return_value=False):
                report = run_audit()

    assert report.flat is False
    assert report.broker_open == 3
    assert report.deploy_allowed is False
    assert report.session_state == "active_session"


@patch("scripts.desk_deploy_audit._http_get")
@patch("scripts.desk_deploy_audit._port_listening", return_value=True)
@patch("scripts.desk_deploy_audit._pgrep")
def test_force_supervised_requires_loop(mock_pgrep, _mock_port, mock_http):
    mock_pgrep.side_effect = lambda pat: {
        "src/main.py": [(100, "main")],
        "runtime.trade_support_wrapper": [],
        "runtime.desk_support_wrapper": [],
        "manage_live_positions.py --supervise": [(200, "supervise-loop")],
    }.get(pat, [])

    mock_http.side_effect = lambda path, timeout=5.0: {
        "/api/positions/live": {"count": 2},
        "/api/position_manager/status": {"active": True, "tick_count": 1},
    }[path]

    with patch("scripts.desk_deploy_audit._pid_alive", return_value=True):
        with patch("scripts.desk_deploy_audit._wrapper_status", return_value={"present": False, "stale": True}):
            with patch("system.shutdown_cleanup.manual_stop_active", return_value=False):
                report = run_audit(force_supervised=True)

    assert report.deploy_allowed is True
    assert report.supervise_loop_running is True
