"""Restart hygiene — manual stop, supervisor dedup, clean slate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from system.shutdown_cleanup import (
    clear_manual_stop,
    manual_stop_active,
    mark_manual_stop,
    stop_daemon_supervisor_processes,
)


@pytest.fixture(autouse=True)
def _clean_manual_stop(tmp_path: Path, monkeypatch):
    stop_file = tmp_path / "state" / "manual_stop.json"
    monkeypatch.setattr(
        "system.shutdown_cleanup._MANUAL_STOP_FILE",
        stop_file,
    )
    clear_manual_stop()
    yield
    clear_manual_stop()


def test_manual_stop_blocks_restart_until_cleared():
    mark_manual_stop(source="test")
    assert manual_stop_active() is True
    clear_manual_stop()
    assert manual_stop_active() is False


def test_stop_daemon_supervisor_noop_under_pytest():
    assert stop_daemon_supervisor_processes() == []


def test_agent_kill_script_mark_before_kill_order():
    text = Path("macos/launcher/agent_kill.sh").read_text(encoding="utf-8")
    mark_pos = text.index("mark_manual_stop")
    term_pos = text.index("TERM — IG Agent process families")
    pre_purge = "pre-purge port"
    assert mark_pos < term_pos
    assert pre_purge not in text


def test_daemon_supervisor_respects_manual_stop():
    text = Path("scripts/daemon_supervisor.sh").read_text(encoding="utf-8")
    assert "manual_stop_engaged" in text
    assert "recovery suppressed — manual_stop active" in text
    assert "manual_stop hold (no auto-restart)" in text


def test_daemon_supervisor_kills_all_main_py_on_recovery():
    text = Path("scripts/daemon_supervisor.sh").read_text(encoding="utf-8")
    assert "SIGTERM all main.py" in text
    assert "evict_stale_processes full" in text
    assert "evict_stale_processes soft" not in text


def test_agent_start_clears_manual_stop_before_supervisor_spawn():
    text = Path("macos/launcher/agent_start.sh").read_text(encoding="utf-8")
    boot_anchor = text.index('log "[AGENT] fresh interpreter via daemon_supervisor"')
    boot_section = text[boot_anchor:]
    clear_pos = boot_section.index("clear_manual_stop")
    supervisor_pos = boot_section.index("nohup \"${IG_AGENT_ROOT}/scripts/daemon_supervisor.sh\"")
    assert clear_pos < supervisor_pos
    preflight_end = text.index('log "[TEST]"')
    assert text[:preflight_end].count("clear_manual_stop") == 0


def test_watchdog_defers_to_daemon_supervisor():
    text = Path("scripts/watchdog.sh").read_text(encoding="utf-8")
    assert "supervisor_managed" in text
    assert "daemon_supervisor active" in text
    assert "supervisor.pid" in text
    assert "daemon_supervisor booting" in text
