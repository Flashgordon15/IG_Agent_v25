"""Background monitor — alert when agent is up but not actually trading."""

from __future__ import annotations

import threading
import time

from system.engine_log import log_engine

_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_STOP = threading.Event()
_UNHEALTHY_STREAK = 0
_LAST_ALERT_MONO = 0.0
_CHECK_INTERVAL_SEC = 60.0
_ALERT_AFTER_STREAK = 5
_ALERT_COOLDOWN_SEC = 900.0


def _check_once() -> None:
    global _UNHEALTHY_STREAK, _LAST_ALERT_MONO
    try:
        from api.agent_control import is_trading_running
        from api.agent_health import build_health_status

        if not is_trading_running():
            _UNHEALTHY_STREAK = 0
            return

        status = build_health_status()
        if status.get("trading_healthy"):
            _UNHEALTHY_STREAK = 0
            return

        _UNHEALTHY_STREAK += 1
        issues = status.get("issues") or []
        detail = ", ".join(str(i) for i in issues) or "unknown"
        log_engine(
            f"trading_health_monitor: UNHEALTHY streak={_UNHEALTHY_STREAK} ({detail})"
        )
        if _UNHEALTHY_STREAK < _ALERT_AFTER_STREAK:
            return
        now = time.monotonic()
        if now - _LAST_ALERT_MONO < _ALERT_COOLDOWN_SEC:
            return
        _LAST_ALERT_MONO = now
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(f"⚠️ Agent unhealthy x{_UNHEALTHY_STREAK} — {detail}")
    except Exception as e:
        log_engine(f"trading_health_monitor error: {type(e).__name__}: {e}")


def start_trading_health_monitor() -> None:
    """Daemon thread: detect zombie trading state and alert via Telegram."""
    global _MONITOR_THREAD
    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return
    _MONITOR_STOP.clear()

    def _loop() -> None:
        while not _MONITOR_STOP.wait(_CHECK_INTERVAL_SEC):
            _check_once()

    _MONITOR_THREAD = threading.Thread(
        target=_loop, name="trading-health-monitor", daemon=True
    )
    _MONITOR_THREAD.start()
    log_engine("trading_health_monitor started")


def stop_trading_health_monitor() -> None:
    _MONITOR_STOP.set()
