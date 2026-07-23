"""Operator controls for safe development while the book may be open.

Two modes (never flatten opens):

1. **Pause trading** (preferred for hotfixes) — freeze new entries via
   ``entry_halt`` + ``trading_paused`` + optional ``deploy_hold``. Agent,
   OPM, and ``trade_support`` stay up and keep supervising opens.

2. **Offline with opens** — anti-zombie stop of ``main.py`` while leaving
   ``trade_support`` (and preferably desk_support) alive. Watchdog held via
   ``mark_manual_stop``. Reload with ``desk_deploy.sh deploy --force-open-book``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from system.paths import data_dir, project_root, state_dir


def _state_path(name: str) -> Path:
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def _under_pytest_or_harness() -> bool:
    return (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _is_production_state_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
        prod = (project_root() / "src" / "data" / "v31-production").resolve()
        return str(resolved).startswith(str(prod) + os.sep) or resolved == prod
    except OSError:
        return False


def _lane_state_roots() -> list[Path]:
    """Shared state/ plus CFD/SB lane mirrors (v32 dual-port)."""
    root = Path(data_dir())
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in (state_dir(), root / "state", root / "state_cfd", root / "state_sb"):
        try:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _write_flag(name: str, *, active: bool, reason: str) -> dict[str, Any]:
    payload = {
        "active": bool(active),
        "reason": str(reason or ""),
        "ts": time.time(),
    }
    text = json.dumps(payload, indent=2)
    primary = _state_path(name)
    if _under_pytest_or_harness() and _is_production_state_path(primary):
        # Never let unit tests stamp pause/hold into the live desk tree.
        return payload
    # Write shared state + lane mirrors so resume clears CFD/SB holds too.
    for root in _lane_state_roots():
        try:
            path = root / name
            if _under_pytest_or_harness() and _is_production_state_path(path):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            continue
    return payload


def _read_flag(name: str) -> dict[str, Any]:
    path = _state_path(name)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def pause_entries(*, reason: str = "desk_dev_pause") -> dict[str, Any]:
    """Freeze new entries without stopping open-position supervision.

    Writes ``entry_halt.json`` + ``trading_paused.json`` and arms deploy_hold.
    Does **not** flatten, does **not** stop trade_support / OPM.
    """
    out: dict[str, Any] = {"ok": True, "mode": "pause_entries", "reason": reason}
    out["entry_halt"] = _write_flag("entry_halt.json", active=True, reason=reason)
    out["trading_paused"] = _write_flag(
        "trading_paused.json", active=True, reason=reason
    )
    try:
        from runtime.deploy_hold import set_deploy_hold

        set_deploy_hold(active=True, reason=reason)
        out["deploy_hold"] = True
    except Exception as exc:
        out["deploy_hold"] = f"{type(exc).__name__}: {exc}"
    # Best-effort in-process API pause (no-op if agent not in this process).
    try:
        from api import agent_control

        with agent_control._lock:
            agent_control._paused = True
        out["api_paused"] = True
    except Exception:
        out["api_paused"] = False
    return out


def resume_entries(*, reason: str = "desk_dev_resume") -> dict[str, Any]:
    """Clear entry holds so path_live can recover (supervisors unchanged).

    Clears ``entry_halt`` / ``trading_paused`` / ``offline_for_dev`` under
    shared ``state/`` **and** lane mirrors ``state_cfd/`` + ``state_sb/``.
    """
    out: dict[str, Any] = {"ok": True, "mode": "resume_entries", "reason": reason}
    out["entry_halt"] = _write_flag("entry_halt.json", active=False, reason=reason)
    out["trading_paused"] = _write_flag(
        "trading_paused.json", active=False, reason=reason
    )
    out["offline_for_dev"] = _write_flag(
        "offline_for_dev.json", active=False, reason=reason
    )
    cleared_lanes: list[str] = []
    for root in _lane_state_roots():
        for name in ("entry_halt.json", "trading_paused.json", "offline_for_dev.json"):
            path = root / name
            if path.is_file():
                cleared_lanes.append(f"{root.name}/{name}")
    out["lanes_cleared"] = cleared_lanes
    try:
        from runtime.deploy_hold import set_deploy_hold

        set_deploy_hold(active=False, reason=reason)
        out["deploy_hold"] = False
    except Exception as exc:
        out["deploy_hold"] = f"{type(exc).__name__}: {exc}"
    try:
        from api import agent_control

        with agent_control._lock:
            agent_control._paused = False
        out["api_paused"] = False
    except Exception:
        pass
    return out


def mark_offline_for_dev(*, reason: str = "desk_dev_offline") -> dict[str, Any]:
    """Flag offline-for-dev + entry pause (call before/after stopping main)."""
    out = pause_entries(reason=reason)
    out["mode"] = "offline_for_dev"
    out["offline_for_dev"] = _write_flag(
        "offline_for_dev.json", active=True, reason=reason
    )
    return out


def entries_paused() -> bool:
    for name in ("entry_halt.json", "trading_paused.json"):
        raw = _read_flag(name)
        if bool(raw.get("active")):
            return True
    return False


def status_snapshot() -> dict[str, Any]:
    flags = {
        name: _read_flag(name)
        for name in (
            "entry_halt.json",
            "trading_paused.json",
            "offline_for_dev.json",
            "deploy_hold.json",
            "manual_stop.json",
        )
    }
    return {
        "entries_paused": entries_paused(),
        "flags": flags,
    }
