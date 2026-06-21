"""Non-blocking services started once Gate 5 marks the agent ACTIVE."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine

_SESSION_REFRESH_INTERVAL_SEC = 45 * 60


def _harness_mode() -> bool:
    return os.environ.get("IG_TEST_HARNESS", "").strip() == "1"


def _start_session_refresh_watchdog(rest_client: Any) -> None:
    if rest_client is None:
        return

    def _refresh_loop() -> None:
        while True:
            time.sleep(_SESSION_REFRESH_INTERVAL_SEC)
            try:
                refreshed = rest_client.proactive_refresh_if_needed()
                if not refreshed:
                    try:
                        rest_client.ensure_session()
                        log_engine("IG session keep-alive: session verified")
                    except Exception as e:
                        log_engine(
                            f"IG session keep-alive failed: {type(e).__name__}: {e}"
                        )
                else:
                    log_engine(
                        "IG session keep-alive: proactive token refresh completed"
                    )
            except Exception as e:
                log_engine(
                    f"IG session refresh watchdog error: {type(e).__name__}: {e}"
                )

    threading.Thread(
        target=_refresh_loop,
        name="ig-session-refresh",
        daemon=True,
    ).start()
    log_engine(
        f"IG session refresh watchdog started (interval {_SESSION_REFRESH_INTERVAL_SEC // 60}m)"
    )


def start_post_ready_services(context: BootContext) -> None:
    """Start schedulers and monitors after dormant loops are unpaused."""
    if _harness_mode():
        log_engine("post-ready: harness fast-path — skipping non-essential daemons")
        return

    cfg = context.config
    rest = context.rest_client

    try:
        from system.replay_daily_scheduler import start_replay_daily_scheduler

        start_replay_daily_scheduler()
    except Exception as e:
        log_engine(f"post-ready: replay scheduler skipped: {type(e).__name__}: {e}")

    try:
        from system.trading_health_monitor import start_trading_health_monitor

        start_trading_health_monitor()
    except Exception as e:
        log_engine(f"post-ready: trading health monitor skipped: {type(e).__name__}: {e}")

    if cfg is not None:
        try:
            from data.learning_store import LearningStore
            from system.setup_registry_refresh import refresh_setup_registry_from_store

            store = LearningStore(str(cfg.learning_db))
            summary = refresh_setup_registry_from_store(store, enabled=True)
            log_engine(
                "setup_registry refreshed at startup: "
                f"banned={summary.get('banned_count')} "
                f"gate={'on' if summary.get('enabled') else 'off'}"
            )
        except Exception as e:
            log_engine(
                f"post-ready: setup_registry refresh skipped: {type(e).__name__}: {e}"
            )

    try:
        from system.gate_coherence_scheduler import start_gate_coherence_scheduler

        start_gate_coherence_scheduler()
    except Exception as e:
        log_engine(f"post-ready: gate coherence scheduler skipped: {type(e).__name__}: {e}")

    try:
        from system.telegram_alerts import start_hourly_executive_telegram_scheduler

        start_hourly_executive_telegram_scheduler()
    except Exception as e:
        log_engine(f"post-ready: telegram scheduler skipped: {type(e).__name__}: {e}")

    try:
        from system.v26_shadow_service import start_v26_shadow_service

        start_v26_shadow_service()
    except Exception as e:
        log_engine(f"post-ready: v26 shadow service skipped: {type(e).__name__}: {e}")

    try:
        from ai.operational.system_monitor import get_system_monitor

        get_system_monitor().run_background()
        log_engine("v27 sentinel monitor started (background)")
    except Exception as e:
        log_engine(f"post-ready: sentinel monitor skipped: {type(e).__name__}: {e}")

    _start_session_refresh_watchdog(rest)

    try:
        from system.alert_dispatcher import start_alert_dispatcher

        start_alert_dispatcher()
    except Exception as e:
        log_engine(f"post-ready: alert dispatcher skipped: {type(e).__name__}: {e}")

    if cfg is not None and cfg.get("intelligence_layer", {}).get("enabled"):
        try:
            from intelligence.intelligence_worker import start_intelligence_worker

            start_intelligence_worker()
            log_engine("post-ready: IntelligenceComputeWorker started")
        except Exception as e:
            log_engine(
                f"post-ready: intelligence worker skipped: {type(e).__name__}: {e}"
            )

    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert("✅ Agent started — trading loops active")
    except Exception as e:
        log_engine(f"post-ready: telegram startup alert failed: {type(e).__name__}: {e}")

    try:
        from system.self_healing_supervisor import start_self_healing_supervisor

        start_self_healing_supervisor()
    except Exception as e:
        log_engine(f"post-ready: self-healing supervisor skipped: {type(e).__name__}: {e}")
