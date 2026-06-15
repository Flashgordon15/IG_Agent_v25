"""Unit tests for shutdown_cleanup orphan-process handling."""

from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

import pytest


def test_kill_other_agent_processes_never_signals_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pgrep matching this process must not SIGTERM/SIGKILL the launcher."""
    monkeypatch.delenv("IG_AGENT_PYTEST", raising=False)

    my_pid = os.getpid()
    pgrep_result = MagicMock()
    pgrep_result.stdout = f"{my_pid}\n"
    pgrep_result.returncode = 0

    kill_calls: list[tuple[int, int]] = []

    def track_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    with (
        patch(
            "system.shutdown_cleanup.subprocess.run",
            return_value=pgrep_result,
        ),
        patch("system.shutdown_cleanup.os.kill", side_effect=track_kill),
        patch("system.shutdown_cleanup.log_engine"),
    ):
        from system.shutdown_cleanup import kill_other_agent_processes

        killed = kill_other_agent_processes(
            exclude_pid=my_pid + 999,
            sigkill_survivors=True,
        )

    assert killed == []
    self_signals = [
        (pid, sig)
        for pid, sig in kill_calls
        if pid == my_pid and sig in (signal.SIGTERM, signal.SIGKILL)
    ]
    assert self_signals == []


def test_should_skip_pid_covers_current_and_exclude() -> None:
    from system.shutdown_cleanup import _should_skip_pid

    my_pid = os.getpid()
    assert _should_skip_pid(my_pid, None) is True
    assert _should_skip_pid(my_pid, my_pid + 1) is True
    assert _should_skip_pid(my_pid + 1, my_pid + 1) is True
    assert _should_skip_pid(my_pid + 1, None) is False


def test_kill_other_agent_processes_still_targets_other_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self is skipped but genuine orphan PIDs are still SIGTERM'd."""
    monkeypatch.delenv("IG_AGENT_PYTEST", raising=False)

    my_pid = os.getpid()
    orphan_pid = 99999
    pgrep_result = MagicMock()
    pgrep_result.stdout = f"{my_pid}\n{orphan_pid}\n"
    pgrep_result.returncode = 0

    kill_calls: list[tuple[int, int]] = []

    def track_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    with (
        patch(
            "system.shutdown_cleanup.subprocess.run",
            return_value=pgrep_result,
        ),
        patch("system.shutdown_cleanup.os.kill", side_effect=track_kill),
        patch("system.shutdown_cleanup.log_engine"),
    ):
        from system.shutdown_cleanup import kill_other_agent_processes

        killed = kill_other_agent_processes(
            exclude_pid=my_pid + 999,
            sigkill_survivors=False,
        )

    assert killed == [orphan_pid]
    assert (my_pid, signal.SIGTERM) not in kill_calls
    assert (orphan_pid, signal.SIGTERM) in kill_calls
