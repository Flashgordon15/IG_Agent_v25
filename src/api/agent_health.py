"""Agent health snapshot for /api/health and dashboard status."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from api.agent_control import get_trading_loop, is_paused, is_trading_running
from system.gate_activity import last_gate_check_by_epic, seconds_since_last_gate_eval
from system.paths import data_dir, logs_dir

_API_HOST = "127.0.0.1"
_API_PORT = 8080
_WATCHDOG_MARKER = "scripts/watchdog.sh"
_WATCHDOG_LAUNCHD_MARKER = "watchdog_launchd.py"
_WATCHDOG_PID_FILE = data_dir() / "watchdog.pid"

_HEALTH_CACHE: dict[str, Any] | None = None
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_REFRESH_STOP = threading.Event()
_HEALTH_REFRESH_THREAD: threading.Thread | None = None
_HEALTH_REFRESH_INTERVAL_SEC = 5.0

_RUNTIME_TICK_FIELDS: dict[str, Any] = {}
_RUNTIME_TICK_LOCK = threading.Lock()


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
    """True when launchd or a manual watchdog process is supervising the agent."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            return True
    except Exception:
        pass
    try:
        if _WATCHDOG_PID_FILE.is_file():
            pid_str = _WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                os.kill(int(pid_str), 0)
                return True
    except (OSError, ValueError):
        pass
    for marker in (_WATCHDOG_MARKER, _WATCHDOG_LAUNCHD_MARKER):
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", marker],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                continue
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
                if marker in cmd:
                    return True
        except Exception:
            continue
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


def _env_scorer_fallback_active() -> bool:
    """True when any market loop's environment scorer is in exception fallback mode."""
    loop = get_trading_loop()
    if loop is None:
        return False
    candidates: list[Any] = []
    if hasattr(loop, "loops"):
        candidates.extend(list(getattr(loop, "loops", []) or []))
    elif hasattr(loop, "_env"):
        candidates.append(loop)
    for epic_loop in candidates:
        env = getattr(epic_loop, "_env", None)
        if env is None:
            continue
        try:
            last = env.last_score()
        except Exception:
            continue
        if getattr(last, "fallback_active", False):
            return True
    return False


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
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        cfg = get_config()
        for _iid, inst in InstrumentRegistry(cfg.as_dict()).get_enabled_with_ids():
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
        from trading.ohlc_readiness import epic_quote_health_exempt

        if epic_quote_health_exempt(epic):
            return True
    except Exception:
        pass
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
    actionable_stale = [e for e in stale_epics if e not in exempt_stale]
    markets_open = _markets_open_count(epics)
    quotes_required = markets_open > 0 and len(actionable_stale) > 0
    quotes_fresh = bool(epics) and (not actionable_stale or not quotes_required)

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
    if quotes_required and actionable_stale:
        issues.append(f"quotes_stale:{','.join(actionable_stale)}")
    elif stale_epics and exempt_stale:
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

    supervision_drift = _supervision_drift_fields()
    env_scorer_fallback = _env_scorer_fallback_active()
    all_issues = list(health["issues"])
    drift_issues = supervision_drift.get("issues") or []
    for item in drift_issues:
        tag = f"supervision:{item}"
        if tag not in all_issues:
            all_issues.append(tag)
    if env_scorer_fallback and "env_scorer_fallback_active" not in all_issues:
        all_issues.append("env_scorer_fallback_active")

    gate_relaxations: dict[str, Any] = {}
    try:
        from system.gate_relaxation import relaxation_snapshot

        gate_relaxations = relaxation_snapshot()
    except Exception:
        pass

    return {
        "ok": trading_healthy and watchdog and supervision_drift.get("ok", True),
        "agent_alive": True,
        "trading_healthy": trading_healthy,
        "gate_relaxations": gate_relaxations,
        "trading_loops_running": loops_running,
        "trading_paused": paused,
        "port_bound": _port_bound(),
        "watchdog_active": watchdog,
        "env_scorer_fallback_active": env_scorer_fallback,
        "quotes_fresh": health["quotes_fresh"],
        "quotes_fresh_count": health["quotes_fresh_count"],
        "quotes_total": health["quotes_total"],
        "markets_open_count": health["markets_open_count"],
        "quotes_required_for_health": health["quotes_required_for_health"],
        "issues": all_issues,
        "last_log_age_sec": log_age,
        "last_gate_check_age_sec": gate_age,
        "markets": _build_market_health(),
        "quote_fresh_by_epic": quote_fresh,
        **_overnight_health_fields(),
        **supervision_drift,
    }


def _build_fast_health_status() -> dict[str, Any]:
    """Cheap in-memory snapshot when the background cache has not warmed yet."""
    gate_age = seconds_since_last_gate_eval()
    loops_running = is_trading_running()
    paused = is_paused()
    per_epic = last_gate_check_by_epic()
    now = time.time()
    markets = [
        {
            "epic": epic,
            "last_gate_check": _format_gate_ts(ts),
            "last_gate_check_age_sec": round(now - ts, 1),
        }
        for epic, ts in sorted(per_epic.items())
    ]
    epics = list(per_epic.keys())
    health = evaluate_trading_health(
        loops_running=loops_running,
        paused=paused,
        gate_age=gate_age,
        epics=epics,
        quote_fresh={},
        log_age=None,
        watchdog=None,
    )
    issues = list(health["issues"])
    if "health_cache_warming" not in issues:
        issues.append("health_cache_warming")
    return {
        "ok": bool(loops_running and not paused),
        "agent_alive": True,
        "trading_healthy": bool(health["trading_healthy"]),
        "trading_loops_running": loops_running,
        "trading_paused": paused,
        "port_bound": _port_bound(),
        "watchdog_active": True,
        "quotes_fresh": bool(health["quotes_fresh"]),
        "quotes_fresh_count": health["quotes_fresh_count"],
        "quotes_total": health["quotes_total"],
        "markets_open_count": health["markets_open_count"],
        "quotes_required_for_health": health["quotes_required_for_health"],
        "issues": issues,
        "last_log_age_sec": None,
        "last_gate_check_age_sec": gate_age,
        "markets": markets,
        "quote_fresh_by_epic": {},
        "supervision_drift_ok": True,
        "supervision_drift": {},
        "supervision_warnings": [],
        "overnight_supervision": {},
        "independent_of_cursor": False,
        "overnight_armed": False,
        "env_scorer_fallback_active": _env_scorer_fallback_active(),
        "gate_relaxations": {},
    }


def get_runtime_tick_fields() -> dict[str, Any]:
    """Cached dashboard fields — refreshed by the health-cache thread only."""
    with _RUNTIME_TICK_LOCK:
        if _RUNTIME_TICK_FIELDS:
            return dict(_RUNTIME_TICK_FIELDS)
    fast = _build_fast_health_status()
    return {
        "last_gate_check_age_sec": fast.get("last_gate_check_age_sec"),
        "quotes_fresh": fast.get("quotes_fresh"),
        "markets_open_count": fast.get("markets_open_count"),
        "trading_healthy": fast.get("trading_healthy"),
        "watchdog_active": fast.get("watchdog_active"),
        "supervision_drift_ok": fast.get("supervision_drift_ok"),
        "supervision_drift": fast.get("supervision_drift"),
        "supervision_warnings": fast.get("supervision_warnings"),
        "overnight_supervision": fast.get("overnight_supervision"),
        "independent_of_cursor": fast.get("independent_of_cursor"),
        "overnight_armed": fast.get("overnight_armed"),
        "env_scorer_fallback_active": fast.get("env_scorer_fallback_active"),
        "gate_relaxations": fast.get("gate_relaxations") or {},
    }


def _update_runtime_tick_fields(status: dict[str, Any]) -> None:
    fields = {
        "last_gate_check_age_sec": status.get("last_gate_check_age_sec"),
        "quotes_fresh": status.get("quotes_fresh"),
        "markets_open_count": status.get("markets_open_count"),
        "trading_healthy": status.get("trading_healthy"),
        "watchdog_active": status.get("watchdog_active"),
        "supervision_drift_ok": status.get("supervision_drift_ok"),
        "supervision_drift": status.get("supervision_drift"),
        "supervision_warnings": status.get("supervision_warnings"),
        "overnight_supervision": status.get("overnight_supervision"),
        "independent_of_cursor": status.get("independent_of_cursor"),
        "overnight_armed": status.get("overnight_armed"),
        "env_scorer_fallback_active": status.get("env_scorer_fallback_active"),
        "gate_relaxations": status.get("gate_relaxations") or {},
    }
    with _RUNTIME_TICK_LOCK:
        global _RUNTIME_TICK_FIELDS
        _RUNTIME_TICK_FIELDS = fields


def refresh_health_cache() -> dict[str, Any]:
    """Rebuild the cached /api/health payload (intended for background threads only)."""
    status = build_health_status()
    with _HEALTH_CACHE_LOCK:
        global _HEALTH_CACHE
        _HEALTH_CACHE = status
    _update_runtime_tick_fields(status)
    return status


def get_cached_health_status() -> dict[str, Any]:
    """Return the latest cached health snapshot without blocking HTTP handlers."""
    with _HEALTH_CACHE_LOCK:
        if _HEALTH_CACHE is not None:
            return dict(_HEALTH_CACHE)
    return _build_fast_health_status()


def start_health_cache_refresher(
    interval_sec: float = _HEALTH_REFRESH_INTERVAL_SEC,
) -> None:
    """Daemon thread: keep /api/health cache fresh under CPU-heavy trading load."""
    global _HEALTH_REFRESH_THREAD
    if _HEALTH_REFRESH_THREAD is not None and _HEALTH_REFRESH_THREAD.is_alive():
        return
    _HEALTH_REFRESH_STOP.clear()

    def _loop() -> None:
        while not _HEALTH_REFRESH_STOP.is_set():
            try:
                refresh_health_cache()
            except Exception:
                pass
            if _HEALTH_REFRESH_STOP.wait(interval_sec):
                break

    _HEALTH_REFRESH_THREAD = threading.Thread(
        target=_loop, name="health-cache-refresher", daemon=True
    )
    _HEALTH_REFRESH_THREAD.start()


def stop_health_cache_refresher() -> None:
    _HEALTH_REFRESH_STOP.set()


def reset_health_cache_for_tests() -> None:
    global _HEALTH_CACHE, _HEALTH_REFRESH_THREAD, _RUNTIME_TICK_FIELDS
    stop_health_cache_refresher()
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE = None
    with _RUNTIME_TICK_LOCK:
        _RUNTIME_TICK_FIELDS = {}
    _HEALTH_REFRESH_THREAD = None
    _HEALTH_REFRESH_STOP.clear()


def _supervision_drift_fields() -> dict[str, Any]:
    try:
        from system.supervision_monitor import evaluate_supervision_drift

        drift = evaluate_supervision_drift()
        return {
            "supervision_drift_ok": bool(drift.get("ok")),
            "supervision_drift": drift,
            "supervision_warnings": drift.get("warnings") or [],
        }
    except Exception:
        return {
            "supervision_drift_ok": True,
            "supervision_drift": {},
            "supervision_warnings": [],
        }


def _overnight_health_fields() -> dict[str, Any]:
    try:
        from system.overnight_supervision import overnight_supervision_summary

        summary = overnight_supervision_summary()
        return {
            "overnight_supervision": summary,
            "independent_of_cursor": bool(summary.get("independent_of_cursor")),
            "overnight_armed": bool(summary.get("overnight_armed")),
        }
    except Exception:
        return {
            "overnight_supervision": {},
            "independent_of_cursor": False,
            "overnight_armed": False,
        }


def stop_watchdog(*, preserve_launchd: bool = True) -> None:
    """
    Stop watchdog processes.

    When preserve_launchd=True (default), leave the launchd supervision job loaded
    so overnight Safe to Leave survives dashboard Stop Agent.
    """
    if not preserve_launchd:
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
            if pid_str.isdigit() and not preserve_launchd:
                subprocess.run(["/bin/kill", "-TERM", pid_str], timeout=3)
                _WATCHDOG_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    if preserve_launchd:
        return

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
