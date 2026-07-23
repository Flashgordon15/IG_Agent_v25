"""session_ready must never SIGKILL a healthy :8080 listener."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch


def _load_session_ready():
    path = Path(__file__).resolve().parents[1] / "scripts" / "session_ready.py"
    spec = importlib.util.spec_from_file_location("session_ready_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kill_tree_refuses_healthy_listener() -> None:
    mod = _load_session_ready()
    with (
        patch.object(mod, "_pid_serves_healthy_api", return_value=True),
        patch.object(mod, "_child_pids", return_value=[]),
        patch.object(mod.os, "kill") as mock_kill,
        patch.object(mod, "_log"),
    ):
        mod._kill_process_tree(39069)
        mock_kill.assert_not_called()


def test_kill_tree_terms_unhealthy() -> None:
    mod = _load_session_ready()
    alive = {"n": 0}

    def fake_kill(pid, sig):
        # After SIGTERM, report dead so we never reach SIGKILL in the wait loop.
        alive["n"] += 1
        if alive["n"] > 1:
            raise OSError("gone")

    with (
        patch.object(mod, "_pid_serves_healthy_api", return_value=False),
        patch.object(mod, "_child_pids", return_value=[]),
        patch.object(mod.os, "kill", side_effect=fake_kill),
        patch.object(mod.time, "sleep"),
        patch.object(mod, "_log"),
    ):
        mod._kill_process_tree(111)
        # At least one kill attempt
        assert alive["n"] >= 1
