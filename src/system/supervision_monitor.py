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
        if len(duplicate) > 1:
            issues.append(f"duplicate_main_py_processes:{len(duplicate)}")

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


def attempt_supervision_repair() -> tuple[bool, str]:
    """Best-effort reload of launchd supervision when plists are installed."""
    try:
        from system.overnight_supervision import ensure_launchd_supervision_loaded

        return ensure_launchd_supervision_loaded()
    except Exception as e:
        return False, f"repair failed: {type(e).__name__}: {e}"


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
