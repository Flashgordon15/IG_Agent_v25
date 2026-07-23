"""Canonical agent / UI pid files under IG_DATA_ROOT.

Launchers historically wrote ``{data_root}/agent.pid`` while some boot paths
only wrote ``{data_root}/state/agent.pid``. Desk relaunches and watchdogs then
saw pid drift. Always mirror both locations.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def _data_root() -> Path:
    try:
        from system.paths import data_dir

        return Path(data_dir())
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "v31-production"


def _state_root() -> Path:
    try:
        from system.paths import state_dir

        return Path(state_dir())
    except Exception:
        return _data_root() / "state"


def agent_pid_paths() -> list[Path]:
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        return [_state_root() / "agent.pid"]
    root = _data_root()
    return [root / "agent.pid", _state_root() / "agent.pid"]


def ui_pid_path() -> Path:
    return _state_root() / "ui.pid"


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(pid)}\n", encoding="utf-8")


def write_agent_pid(pid: int | None = None) -> list[str]:
    """Mirror live agent pid to data_root and state_dir. Returns written paths."""
    want = int(pid or os.getpid())
    written: list[str] = []
    for path in agent_pid_paths():
        try:
            _write_pid(path, want)
            written.append(str(path))
        except Exception:
            continue
    return written


def write_ui_pid(pid: int | None = None) -> str | None:
    """Write Quantum Terminal / pywebview shell pid."""
    want = int(pid or os.getpid())
    path = ui_pid_path()
    try:
        _write_pid(path, want)
        return str(path)
    except Exception:
        return None


def read_agent_pid() -> int | None:
    for path in agent_pid_paths():
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                return int(raw)
        except Exception:
            continue
    return None


def clear_stale_pids(*, keep: Iterable[str] | None = None) -> None:
    """Best-effort unlink pid files (shutdown)."""
    keep_set = {str(p) for p in (keep or ())}
    for path in [*agent_pid_paths(), ui_pid_path()]:
        if str(path) in keep_set:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
