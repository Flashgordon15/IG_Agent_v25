"""Supervision drift detection and optional self-repair — the agent's built-in operator."""

from __future__ import annotations

import time
from typing import Any

from system.engine_log import log_engine

_LAST_TICK_MONO = 0.0
_LAST_ALERT_MONO = 0.0
_LAST_ISSUE_SIGNATURE = ""
_TICK_INTERVAL_SEC = 60.0
_ALERT_COOLDOWN_SEC = 900.0


def _agent_listening(port: int = 8080) -> bool:
    try:
        from system.overnight_supervision import _listener_pid

        return _listener_pid(port=port) is not None
    except Exception:
        return False


def evaluate_supervision_drift(*, port: int = 8080) -> dict[str, Any]:
    """
    Return structured supervision health for /api/health and AI operators.

    Issues = actionable failures. Warnings = drift that may become failures.
    """
    issues: list[str] = []
    warnings: list[str] = []
    repairs_attempted: list[str] = []

    try:
        from system.overnight_supervision import (
            agent_process_supervision_status,
            overnight_supervision_summary,
        )
        from system.shutdown_cleanup import manual_stop_active

        summary = overnight_supervision_summary(port=port)
        launchd_wd = bool(summary.get("launchd_watchdog"))
        armed = bool(summary.get("overnight_armed"))
        agent_up = _agent_listening(port=port)

        try:
            from api.agent_health import _watchdog_active

            watchdog_proc = _watchdog_active()
        except Exception:
            watchdog_proc = False

        if armed and not launchd_wd:
            issues.append("overnight_armed_but_launchd_watchdog_missing")

        if agent_up and not launchd_wd and not watchdog_proc:
            issues.append("agent_running_without_watchdog")

        agent_ok, agent_detail = agent_process_supervision_status(port=port)
        if agent_up and not agent_ok and not launchd_wd:
            issues.append(f"agent_fragile_supervision:{agent_detail[:120]}")

        if manual_stop_active() and not agent_up:
            warnings.append("manual_stop_active_agent_down")

        if manual_stop_active() and agent_up and launchd_wd:
            warnings.append("manual_stop_active_while_agent_running")

        if launchd_wd and not watchdog_proc:
            warnings.append("launchd_watchdog_job_loaded_but_process_not_detected")

        duplicate = _duplicate_main_pids()
        v32_dual = _v32_dual_supervision_expected()
        if len(duplicate) > 1 and not v32_dual:
            issues.append(f"duplicate_main_py_processes:{len(duplicate)}")
        elif len(duplicate) > 1 and v32_dual and not _both_v32_ports_listening():
            warnings.append(f"v32_dual_processes_unhealthy:{len(duplicate)}")

        try:
            from system.shutdown_cleanup import supervision_utility_permission_issues

            blocked = supervision_utility_permission_issues()
            if blocked:
                issues.append(
                    "supervision_scripts_not_executable:"
                    + ",".join(blocked[:5])
                    + ("…" if len(blocked) > 5 else "")
                )
        except Exception:
            pass

    except Exception as e:
        issues.append(f"supervision_eval_error:{type(e).__name__}")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "repairs_attempted": repairs_attempted,
        "ts": time.time(),
    }


def _duplicate_main_pids() -> list[int]:
    import subprocess

    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in (result.stdout or "").strip().splitlines():
            if line.strip().isdigit():
                pids.append(int(line.strip()))
    except Exception:
        pass
    return pids


def _port_health_ok(port: int, *, timeout_sec: float = 2.0) -> bool:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-Agent-Supervision/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _both_v32_ports_listening() -> bool:
    """True when intentional v32 twin (:8080 + :8081) both respond to /api/health."""
    import os

    marker = os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
    cfd_ok = _port_health_ok(8080)
    sb_ok = _port_health_ok(8081)
    if marker and cfd_ok and sb_ok:
        return True
    return cfd_ok and sb_ok


def _v32_dual_supervision_expected() -> bool:
    import os
    from pathlib import Path

    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        return True
    try:
        from system.paths import data_dir

        root = Path(data_dir())
        if (root / "state" / "v32_dual_supervision.json").is_file():
            return True
        if (root / "state" / "v32_legacy_watchdog_paused.json").is_file():
            return True
    except Exception:
        pass
    return _both_v32_ports_listening()


def attempt_supervision_repair() -> tuple[bool, str]:
    """Best-effort reload of launchd supervision when plists are installed."""
    notes: list[str] = []
    try:
        from system.shutdown_cleanup import ensure_supervision_utilities_executable

        util_ok, repaired = ensure_supervision_utilities_executable()
        if repaired:
            notes.append("chmod +x " + ", ".join(repaired[:3]))
        if not util_ok:
            notes.append("supervision utilities still not executable")
    except Exception as e:
        notes.append(f"chmod repair skipped: {type(e).__name__}")
    try:
        from system.overnight_supervision import ensure_launchd_supervision_loaded

        ok, detail = ensure_launchd_supervision_loaded()
        if ok:
            notes.append(detail)
            return True, "; ".join(notes) if notes else detail
        notes.append(detail)
    except Exception as e:
        notes.append(f"repair failed: {type(e).__name__}: {e}")
    return False, "; ".join(notes) if notes else "repair failed"


def run_supervision_monitor_tick(*, repair: bool = True) -> dict[str, Any]:
    """
    Periodic operator tick — log drift, alert on sustained issues, optional self-heal.
    Called from trading_health_monitor while the agent is running.
    """
    global _LAST_TICK_MONO, _LAST_ALERT_MONO, _LAST_ISSUE_SIGNATURE

    now = time.monotonic()
    if now - _LAST_TICK_MONO < _TICK_INTERVAL_SEC:
        return {"skipped": True}
    _LAST_TICK_MONO = now

    drift = evaluate_supervision_drift()
    issues = list(drift.get("issues") or [])
    warnings = list(drift.get("warnings") or [])

    if repair and "overnight_armed_but_launchd_watchdog_missing" in issues:
        ok, detail = attempt_supervision_repair()
        drift["repairs_attempted"] = [detail]
        log_engine(f"supervision_monitor: auto-repair launchd → ok={ok} ({detail})")
        if ok:
            drift = evaluate_supervision_drift()
            issues = list(drift.get("issues") or [])

    signature = "|".join(sorted(issues + warnings))
    if issues:
        log_engine(
            "supervision_monitor: ISSUES "
            + ", ".join(issues)
            + (f" | warnings: {', '.join(warnings)}" if warnings else "")
        )
    elif warnings:
        log_engine(f"supervision_monitor: warnings {', '.join(warnings)}")

    if issues and signature != _LAST_ISSUE_SIGNATURE:
        _LAST_ISSUE_SIGNATURE = signature
        if now - _LAST_ALERT_MONO >= _ALERT_COOLDOWN_SEC:
            _LAST_ALERT_MONO = now
            try:
                from system.telegram_notifier import send_critical_alert

                send_critical_alert(
                    "🛡 Supervision drift — "
                    + ", ".join(issues[:3])
                    + (" …" if len(issues) > 3 else "")
                )
            except Exception:
                pass

    drift["skipped"] = False
    return drift


def reset_supervision_monitor_for_tests() -> None:
    global _LAST_TICK_MONO, _LAST_ALERT_MONO, _LAST_ISSUE_SIGNATURE
    _LAST_TICK_MONO = 0.0
    _LAST_ALERT_MONO = 0.0
    _LAST_ISSUE_SIGNATURE = ""
