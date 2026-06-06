"""Agent health snapshot for /api/health and dashboard status."""

from __future__ import annotations

import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from api.agent_control import get_trading_loop, is_paused, is_trading_running
from system.gate_activity import last_gate_check_by_epic, seconds_since_last_gate_eval
from system.paths import logs_dir, project_root

_API_HOST = "127.0.0.1"
_API_PORT = 8080
_WATCHDOG_MARKER = "scripts/watchdog.sh"


def _port_bound(port: int = _API_PORT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return s.connect_ex((_API_HOST, port)) == 0
    finally:
        s.close()


def _engine_log_age_sec() -> float | None:
    try:
        log_path = logs_dir() / "engine.log"
        if not log_path.is_file():
            return None
        return max(0.0, time.time() - log_path.stat().st_mtime)
    except Exception:
        return None


def _watchdog_active() -> bool:
    """True when our self-healing watchdog.sh process is running."""
    try:
        root = str(project_root())
        result = subprocess.run(
            ["pgrep", "-f", _WATCHDOG_MARKER],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.strip().splitlines():
            pid_str = line.strip()
            if not pid_str.isdigit():
                continue
            proc = subprocess.run(
                ["ps", "-p", pid_str, "-o", "args="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            cmd = (proc.stdout or "").strip()
            if _WATCHDOG_MARKER in cmd and root in cmd:
                return True
        return False
    except Exception:
        return False


def _format_gate_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[
            :-3
        ]
        + "Z"
    )


def _build_market_health() -> list[dict[str, Any]]:
    now = time.time()
    per_epic = last_gate_check_by_epic()
    loop = get_trading_loop()
    markets: list[dict[str, Any]] = []

    if loop is not None and hasattr(loop, "loops"):
        for epic_loop in loop.loops:
            epic = str(getattr(epic_loop, "_epic", "") or "")
            ts = per_epic.get(epic)
            markets.append(
                {
                    "epic": epic,
                    "last_gate_check": _format_gate_ts(ts),
                    "last_gate_check_age_sec": (
                        round(now - ts, 1) if ts is not None else None
                    ),
                }
            )
    elif loop is not None:
        epic = str(getattr(loop, "_epic", "") or "")
        ts = per_epic.get(epic)
        markets.append(
            {
                "epic": epic,
                "last_gate_check": _format_gate_ts(ts),
                "last_gate_check_age_sec": (
                    round(now - ts, 1) if ts is not None else None
                ),
            }
        )
    else:
        for epic, ts in sorted(per_epic.items()):
            markets.append(
                {
                    "epic": epic,
                    "last_gate_check": _format_gate_ts(ts),
                    "last_gate_check_age_sec": round(now - ts, 1),
                }
            )
    return markets


def build_health_status() -> dict[str, Any]:
    gate_age = seconds_since_last_gate_eval()
    return {
        "ok": True,
        # Responding to /api/health proves the agent process is alive.
        "agent_alive": True,
        "trading_loops_running": is_trading_running(),
        "trading_paused": is_paused(),
        "port_bound": _port_bound(),
        "watchdog_active": _watchdog_active(),
        "last_log_age_sec": _engine_log_age_sec(),
        "last_gate_check_age_sec": gate_age,
        "markets": _build_market_health(),
    }


def stop_watchdog() -> None:
    """SIGTERM the project watchdog so explicit Stop does not auto-restart."""
    try:
        root = str(project_root())
        result = subprocess.run(
            ["pgrep", "-f", _WATCHDOG_MARKER],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return
        for line in result.stdout.strip().splitlines():
            pid_str = line.strip()
            if not pid_str.isdigit():
                continue
            proc = subprocess.run(
                ["ps", "-p", pid_str, "-o", "args="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            cmd = (proc.stdout or "").strip()
            if _WATCHDOG_MARKER in cmd and root in cmd:
                subprocess.run(["kill", "-TERM", pid_str], timeout=3)
    except Exception:
        pass
