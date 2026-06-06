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
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", _WATCHDOG_MARKER],
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
                ["/bin/ps", "-p", pid_str, "-o", "args="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            cmd = (proc.stdout or "").strip()
            # pgrep matches full path; ps may show relative "bash scripts/watchdog.sh"
            if _WATCHDOG_MARKER in cmd:
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


def _configured_epics() -> list[str]:
    epics: list[str] = []
    loop = get_trading_loop()
    if loop is not None and hasattr(loop, "loops"):
        for epic_loop in loop.loops:
            epic = str(getattr(epic_loop, "_epic", "") or "").strip()
            if epic:
                epics.append(epic)
    elif loop is not None:
        epic = str(getattr(loop, "_epic", "") or "").strip()
        if epic:
            epics.append(epic)
    if epics:
        return epics
    try:
        from system.config_loader import get_config_loader

        cfg = get_config_loader().load()
        for _iid, inst in (cfg.instruments or {}).items():
            if not inst.get("enabled", True):
                continue
            epic = str(inst.get("epic") or "").strip()
            if epic:
                epics.append(epic)
    except Exception:
        pass
    return epics


def _quotes_fresh_by_epic(
    epics: list[str], *, max_age: float = 45.0
) -> dict[str, bool]:
    from system.rest_api_budget import hub_quote_stream_fresh

    return {epic: hub_quote_stream_fresh(epic=epic, max_age=max_age) for epic in epics}


def build_health_status() -> dict[str, Any]:
    gate_age = seconds_since_last_gate_eval()
    loops_running = is_trading_running()
    paused = is_paused()
    watchdog = _watchdog_active()
    log_age = _engine_log_age_sec()
    epics = _configured_epics()
    quote_fresh = _quotes_fresh_by_epic(epics) if epics else {}
    fresh_count = sum(1 for ok in quote_fresh.values() if ok)
    quotes_fresh = bool(epics) and fresh_count == len(epics)

    issues: list[str] = []
    if not loops_running:
        issues.append("trading_loops_not_running")
    if paused:
        issues.append("trading_paused")
    if not watchdog:
        issues.append("watchdog_inactive")
    if gate_age is None:
        issues.append("no_gate_activity_recorded")
    elif gate_age > 120.0:
        issues.append(f"gate_check_stale_{int(gate_age)}s")
    if epics and not quotes_fresh:
        stale = [e for e, ok in quote_fresh.items() if not ok]
        issues.append(f"quotes_stale:{','.join(stale)}")
    if log_age is not None and log_age > 300.0:
        issues.append(f"engine_log_stale_{int(log_age)}s")

    trading_healthy = (
        loops_running
        and not paused
        and gate_age is not None
        and gate_age <= 120.0
        and quotes_fresh
    )

    return {
        "ok": trading_healthy and watchdog,
        "agent_alive": True,
        "trading_healthy": trading_healthy,
        "trading_loops_running": loops_running,
        "trading_paused": paused,
        "port_bound": _port_bound(),
        "watchdog_active": watchdog,
        "quotes_fresh": quotes_fresh,
        "quotes_fresh_count": fresh_count,
        "quotes_total": len(epics),
        "issues": issues,
        "last_log_age_sec": log_age,
        "last_gate_check_age_sec": gate_age,
        "markets": _build_market_health(),
        "quote_fresh_by_epic": quote_fresh,
    }


def stop_watchdog() -> None:
    """SIGTERM the project watchdog so explicit Stop does not auto-restart."""
    try:
        root = str(project_root())
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", _WATCHDOG_MARKER],
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
                ["/bin/ps", "-p", pid_str, "-o", "args="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            cmd = (proc.stdout or "").strip()
            if _WATCHDOG_MARKER in cmd:
                subprocess.run(["/bin/kill", "-TERM", pid_str], timeout=3)
    except Exception:
        pass
