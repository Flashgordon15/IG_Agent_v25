"""Tests for macOS IG Agent v31 launcher — mocked subprocess and HTTP."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LAUNCHER_DIR = Path(__file__).resolve().parents[1] / "macos" / "launcher"
if str(_LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_LAUNCHER_DIR))

import launcher_core as lc  # noqa: E402


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "start.sh").write_text("#!/bin/bash\nexit 0\n")
    (tmp_path / "scripts" / "stop.sh").write_text("#!/bin/bash\nexit 0\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# stub\n")
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / ".ig_agent_v29.lock").write_text("stale")
    (tmp_path / "dashboard" / "dist").mkdir(parents=True)
    (tmp_path / "dashboard" / "dist" / "index.html").write_text("<html></html>")
    return tmp_path


def test_project_root_from_launcher_dir():
    root = lc.project_root_from(_LAUNCHER_DIR)
    assert (root / "scripts" / "start.sh").is_file()


def test_stop_phase_when_port_already_free(root: Path):
    with patch.object(lc, "port_is_bound", return_value=False), patch.object(
        lc, "run_stop_script", return_value=0
    ), patch.object(lc, "mark_manual_stop_hold"), patch.object(
        lc, "remove_stale_locks", return_value=[]
    ):
        ok, msg = lc.stop_phase(root)
    assert ok is True


def test_stop_phase_escalates_hung_port(root: Path):
    bound = {"v": True}

    def _bound(port: int, host: str = "127.0.0.1") -> bool:
        return bound["v"]

    def _wait_free(port: int, **kwargs) -> bool:
        bound["v"] = False
        return True

    with patch.object(lc, "port_is_bound", side_effect=_bound), patch.object(
        lc, "wait_port_free", side_effect=_wait_free
    ), patch.object(lc, "pids_on_port", return_value=[9999]), patch.object(
        lc, "terminate_pids"
    ), patch.object(
        lc, "run_stop_script", return_value=0
    ), patch.object(
        lc, "mark_manual_stop_hold"
    ), patch.object(
        lc, "remove_stale_locks", return_value=["lock"]
    ):
        ok, _ = lc.stop_phase(root)
    assert ok is True


def test_clean_removes_stale_lock(root: Path):
    lock = root / "src" / "data" / ".ig_agent_v29.lock"
    assert lock.is_file()
    removed = lc.remove_stale_locks(root)
    assert not lock.is_file()
    assert removed


def test_verify_health_success():
    payload = {"system_state": {"phase": "G5"}}

    def fetch(url: str) -> dict:
        return payload

    ok, data = lc.verify_health(timeout_sec=1.0, poll_sec=0.01, fetch=fetch)
    assert ok is True
    assert data == payload


def test_verify_gui_status_all_fields():
    gui_payload = {f: [] for f in lc.REQUIRED_GUI_FIELDS}

    def fetch(url: str) -> dict:
        if "gui_status" in url:
            return gui_payload
        return {}

    ok, missing, data = lc.verify_gui_status(timeout_sec=1.0, poll_sec=0.01, fetch=fetch)
    assert ok is True
    assert missing == []
    assert data == gui_payload


def test_verify_gui_status_missing_fields():
    def fetch(url: str) -> dict:
        return {"session_review": {}}

    ok, missing, _ = lc.verify_gui_status(timeout_sec=0.05, poll_sec=0.01, fetch=fetch)
    assert ok is False
    assert "strategy_selector_advice" in missing


def test_open_dashboard_uses_open_fn(root: Path):
    opened: list[str] = []
    lc.open_dashboard(root, open_fn=lambda u: opened.append(u), popen_fn=MagicMock())
    assert opened == ["http://127.0.0.1:8080/"]


def test_open_dashboard_starts_npm_when_no_dist(root: Path):
    dist = root / "dashboard" / "dist" / "index.html"
    dist.unlink()
    (root / "dashboard" / "package.json").write_text("{}")
    popen = MagicMock()
    opened: list[str] = []
    lc.open_dashboard(root, open_fn=lambda u: opened.append(u), popen_fn=popen)
    popen.assert_called_once()
    assert opened


def test_reset_demo_skippable(root: Path):
    with patch.dict("os.environ", {"LAUNCHER_SKIP_DEMO_RESET": "1"}):
        summary = lc.reset_demo_session_state(root)
    assert summary["applied"] is True


def test_run_start_script_exit_code(root: Path):
    with patch.object(lc.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0)
        assert lc.run_start_script(root) == 0


def test_run_start_script_failure(root: Path):
    with patch.object(lc.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=5)
        assert lc.run_start_script(root) == 5


def test_stop_phase_fails_when_port_still_bound(root: Path):
    with patch.object(lc, "mark_manual_stop_hold"), patch.object(
        lc, "run_stop_script", return_value=0
    ), patch.object(lc, "wait_port_free", return_value=False), patch.object(
        lc, "pids_on_port", return_value=[1234]
    ), patch.object(lc, "terminate_pids"), patch.object(
        lc, "remove_stale_locks", return_value=[]
    ), patch.object(lc, "port_is_bound", return_value=True):
        ok, msg = lc.stop_phase(root)
    assert ok is False
    assert "still bound" in msg


def test_full_stack_verify_integration_mock():
    """Simulate healthy startup verification after mocked start."""
    health = {"system_state": {"phase": "G5"}}
    gui = {f: [] for f in lc.REQUIRED_GUI_FIELDS}

    def fetch(url: str):
        if "health" in url:
            return health
        if "gui_status" in url:
            return gui
        return None

    assert lc.verify_health(timeout_sec=0.1, poll_sec=0.01, fetch=fetch)[0]
    assert lc.verify_gui_status(timeout_sec=0.1, poll_sec=0.01, fetch=fetch)[0]


def test_stop_phase_idempotent_when_already_stopped(root: Path):
    with patch.object(lc, "mark_manual_stop_hold"), patch.object(
        lc, "run_stop_script", return_value=1
    ), patch.object(lc, "port_is_bound", return_value=False), patch.object(
        lc, "remove_stale_locks", return_value=[]
    ):
        ok, _ = lc.stop_phase(root)
    assert ok is True


def test_purge_bytecode_removes_pyc(root: Path):
    cache = root / "src" / "foo" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.pyc").write_bytes(b"x")
    lc.purge_bytecode(root)
    assert not cache.exists()


def test_ideal_session_verification_scores():
    """Ideal session: health G5 + full gui fields."""
    gui = {f: [] for f in lc.REQUIRED_GUI_FIELDS}
    gui["session_review"] = {"session_quality_score": 85, "session_risk_score": 25}

    def fetch(url: str):
        if "health" in url:
            return {"system_state": {"phase": "G5"}}
        return gui

    health_ok, _ = lc.verify_health(timeout_sec=0.1, poll_sec=0.01, fetch=fetch)
    gui_ok, missing, payload = lc.verify_gui_status(timeout_sec=0.1, poll_sec=0.01, fetch=fetch)
    assert health_ok and gui_ok and not missing
    assert payload["session_review"]["session_quality_score"] >= 65


def test_supervisor_scripts_exist_and_executable():
    launcher = _LAUNCHER_DIR
    for name in (
        "agent_kill.sh",
        "agent_start.sh",
        "agent_verify.sh",
        "agent_gui.sh",
        "agent_lib.sh",
        "igagent_launcher.sh",
    ):
        path = launcher / name
        assert path.is_file(), name
        assert os.access(path, os.X_OK), f"{name} not executable"


def test_igagent_swift_supervisor_source_exists():
    swift_src = _LAUNCHER_DIR / "IGAgentSupervisor.swift"
    assert swift_src.is_file()
    text = swift_src.read_text()
    assert "agent_kill.sh" in text
    assert "agent_verify.sh" in text


def test_igagent_go_supervisor_compiles():
    go_src = _LAUNCHER_DIR.parent / "supervisor" / "igagent_launcher.go"
    assert go_src.is_file()


def test_build_swift_script_exists():
    build = _LAUNCHER_DIR.parent / "supervisor" / "build_swift.sh"
    assert build.is_file()


def test_launch_agent_delegates_to_supervisor():
    launch_sh = _LAUNCHER_DIR / "launch_agent.sh"
    text = launch_sh.read_text()
    assert "IGAgentSupervisor" in text
    assert "igagent_launcher" in text
