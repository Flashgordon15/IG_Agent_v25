"""Agent health snapshot for /api/health and dashboard status."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from api.agent_control import get_trading_loop, is_paused, is_trading_running
from system.gate_activity import last_gate_check_by_epic, seconds_since_last_gate_eval
from system.paths import data_dir, logs_dir

_API_HOST = "127.0.0.1"
_API_PORT = 8080
_WATCHDOG_MARKER = "scripts/watchdog.sh"
_WATCHDOG_PID_FILE = data_dir() / "watchdog.pid"


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
        if _WATCHDOG_PID_FILE.is_file():
            pid_str = _WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                os.kill(int(pid_str), 0)
                return True
    except (OSError, ValueError):
        pass
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


def _epic_quote_exempt(epic: str) -> bool:
    """True when stale quotes for this epic are expected (closed or maintenance)."""
    try:
        from system.market_watch.calendar import is_market_open
        from system.market_watch.japan225_session import (
            is_hub_price_maintenance,
            is_scheduled_daily_maintenance,
        )

        if not is_market_open(epic):
            return True
        if is_scheduled_daily_maintenance(epic) or is_hub_price_maintenance(epic):
            return True
    except Exception:
        pass
    return False


def _markets_open_count(epics: list[str]) -> int:
    """How many configured epics are in an IG-open session right now."""
    if not epics:
        return 0
    try:
        from system.market_watch.calendar import is_market_open

        return sum(1 for epic in epics if is_market_open(epic))
    except Exception:
        return len(epics)


def evaluate_trading_health(
    *,
    loops_running: bool,
    paused: bool,
    gate_age: float | None,
    epics: list[str],
    quote_fresh: dict[str, bool],
    log_age: float | None = None,
    watchdog: bool | None = None,
) -> dict[str, Any]:
    """Shared trading-health rules for /api/health and dashboard ticks."""
    fresh_count = sum(1 for ok in quote_fresh.values() if ok)
    stale_epics = [e for e in epics if not quote_fresh.get(e, False)]
    exempt_stale = [e for e in stale_epics if _epic_quote_exempt(e)]
    markets_open = _markets_open_count(epics)
    quotes_required = markets_open > 0 and len(exempt_stale) < len(stale_epics)
    quotes_fresh = bool(epics) and (not stale_epics or not quotes_required)

    issues: list[str] = []
    if not loops_running:
        issues.append("trading_loops_not_running")
    if paused:
        issues.append("trading_paused")
    if watchdog is False:
        issues.append("watchdog_inactive")
    if gate_age is None:
        issues.append("no_gate_activity_recorded")
    elif gate_age > 120.0:
        issues.append(f"gate_check_stale_{int(gate_age)}s")
    if quotes_required and stale_epics:
        actionable = [e for e in stale_epics if e not in exempt_stale]
        if actionable:
            issues.append(f"quotes_stale:{','.join(actionable)}")
        elif stale_epics:
            issues.append(f"quotes_maintenance:{','.join(stale_epics)}")
    if log_age is not None and log_age > 300.0:
        issues.append(f"engine_log_stale_{int(log_age)}s")

    trading_healthy = (
        loops_running
        and not paused
        and gate_age is not None
        and gate_age <= 120.0
        and (quotes_fresh or not quotes_required)
    )

    return {
        "trading_healthy": trading_healthy,
        "quotes_fresh": quotes_fresh,
        "quotes_fresh_count": fresh_count,
        "quotes_total": len(epics),
        "markets_open_count": markets_open,
        "quotes_required_for_health": quotes_required,
        "quotes_maintenance_epics": exempt_stale,
        "issues": issues,
    }


def build_health_status() -> dict[str, Any]:
    gate_age = seconds_since_last_gate_eval()
    loops_running = is_trading_running()
    paused = is_paused()
    watchdog = _watchdog_active()
    log_age = _engine_log_age_sec()
    epics = _configured_epics()
    quote_fresh = _quotes_fresh_by_epic(epics) if epics else {}

    health = evaluate_trading_health(
        loops_running=loops_running,
        paused=paused,
        gate_age=gate_age,
        epics=epics,
        quote_fresh=quote_fresh,
        log_age=log_age,
        watchdog=watchdog,
    )
    trading_healthy = bool(health["trading_healthy"])

    return {
        "ok": trading_healthy and watchdog,
        "agent_alive": True,
        "trading_healthy": trading_healthy,
        "trading_loops_running": loops_running,
        "trading_paused": paused,
        "port_bound": _port_bound(),
        "watchdog_active": watchdog,
        "quotes_fresh": health["quotes_fresh"],
        "quotes_fresh_count": health["quotes_fresh_count"],
        "quotes_total": health["quotes_total"],
        "markets_open_count": health["markets_open_count"],
        "quotes_required_for_health": health["quotes_required_for_health"],
        "issues": health["issues"],
        "last_log_age_sec": log_age,
        "last_gate_check_age_sec": gate_age,
        "markets": _build_market_health(),
        "quote_fresh_by_epic": quote_fresh,
    }


def stop_watchdog() -> None:
    """SIGTERM the project watchdog so explicit Stop does not auto-restart."""
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/com.igagent.v25.watchdog"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        pass
    try:
        if _WATCHDOG_PID_FILE.is_file():
            pid_str = _WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                subprocess.run(["/bin/kill", "-TERM", pid_str], timeout=3)
            _WATCHDOG_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
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
    for sig in ("-TERM", "-KILL"):
        try:
            subprocess.run(
                ["/usr/bin/pkill", sig, "-f", _WATCHDOG_MARKER],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            pass
    try:
        _WATCHDOG_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
