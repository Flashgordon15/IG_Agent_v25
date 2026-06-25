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

    try:
        from system.guard.kernel_interceptor import install_kernel_interceptor

        summary = install_kernel_interceptor()
        log_engine(
            "post-ready: KernelInterceptor armed "
            f"(trading_wrapped={summary.get('trading_wrapped')} "
            f"execution_wrapped={summary.get('execution_wrapped')})"
        )
    except Exception as exc:
        log_engine(
            f"post-ready: KernelInterceptor deferred — {type(exc).__name__}: {exc}"
        )

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

    try:
        from intelligence.matrix_prebaker import (
            fast_bootstrap_alpha_matrix_if_empty,
            start_alpha_matrix_compiler_async,
        )

        def _alpha_matrix_bootstrap() -> None:
            try:
                fast_bootstrap_alpha_matrix_if_empty(stride=48)
                start_alpha_matrix_compiler_async()
                log_engine(
                    "post-ready: AlphaMatrixPrebaker fast bootstrap + async compiler started"
                )
            except Exception as exc:
                log_engine(
                    f"post-ready: alpha matrix prebaker failed: {type(exc).__name__}: {exc}"
                )

        threading.Thread(
            target=_alpha_matrix_bootstrap,
            name="post-ready-alpha-matrix",
            daemon=True,
        ).start()
        log_engine("post-ready: AlphaMatrixPrebaker bootstrap scheduled (background)")
    except Exception as e:
        log_engine(f"post-ready: alpha matrix prebaker skipped: {type(e).__name__}: {e}")

    try:
        from system.cockpit_session_monitor import start_cockpit_session_monitor

        start_cockpit_session_monitor()
    except Exception as e:
        log_engine(f"post-ready: cockpit session monitor skipped: {type(e).__name__}: {e}")

    try:
        from system.cockpit_feed_guardian_agent import start_agent_feed_guardian

        start_agent_feed_guardian()
    except Exception as e:
        log_engine(f"post-ready: agent feed guardian skipped: {type(e).__name__}: {e}")

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
            if rest is not None:
                def _boot_lifecycle() -> None:
                    try:
                        from runtime.active_lifecycle_trades import (
                            boot_reconcile_active_trades,
                        )

                        counts = boot_reconcile_active_trades(rest, store)
                        log_engine(
                            "post-ready: active lifecycle boot reconcile "
                            f"adopted={counts.get('adopted', 0)} "
                            f"synced={counts.get('synced', 0)} "
                            f"broker_open={counts.get('synced', 0) + counts.get('adopted', 0)}"
                        )
                    except Exception as exc:
                        log_engine(
                            f"post-ready: active lifecycle boot failed: "
                            f"{type(exc).__name__}: {exc}"
                        )

                threading.Thread(
                    target=_boot_lifecycle,
                    name="active-lifecycle-boot",
                    daemon=True,
                ).start()
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
        from analytics.post_open_audit import start_post_open_audit_hub

        start_post_open_audit_hub(hourly=True)
    except Exception as e:
        log_engine(f"post-ready: post-open audit hub skipped: {type(e).__name__}: {e}")

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

    if rest is not None:
        try:
            from cockpit.emergency import clear_emergency_cockpit_override

            clear_emergency_cockpit_override(resume_trading=True)
            log_engine("post-ready: COCKPIT_EMERGENCY_OVERRIDE purged — Gate 5 valve open")
        except Exception as e:
            log_engine(f"post-ready: cockpit override purge skipped: {type(e).__name__}: {e}")

        try:
            from runtime.trade_manager import start_dual_core_coordinator

            start_dual_core_coordinator(rest, config=cfg)
            log_engine("post-ready: DualCoreCoordinator ENGINE_B_MICRO_SCALPER armed")
            from runtime.dual_core_execution import (
                lock_forex_rotation_session,
                start_stacked_dual_asset_tracks,
            )

            dual_cfg = (cfg.get("dual_core") or {}) if cfg is not None else {}
            if dual_cfg.get("forex_rotation_locked"):
                lock_forex_rotation_session(
                    reason=str(dual_cfg.get("lock_reason") or "config_forex_rotation_locked")
                )
                log_engine(
                    "post-ready: ForexRotationLock EUR/USD + GBP/USD "
                    "(indices/metals dropped from hot path)"
                )
            start_stacked_dual_asset_tracks()
            log_engine("post-ready: StackedDualAsset parallel tracks armed")
            from runtime.dual_core_execution import start_socket_heartbeat_validator

            start_socket_heartbeat_validator(interval_sec=1.0)
            log_engine("post-ready: SocketHeartbeat validator armed (5s stale → rehydrate)")
            from runtime.virtual_stop_loss import start_virtual_stop_watchdog

            start_virtual_stop_watchdog(rest)
            log_engine("post-ready: VirtualStop 2.0pt watchdog armed (500ms)")
        except Exception as e:
            log_engine(
                f"post-ready: dual-core coordinator skipped: {type(e).__name__}: {e}"
            )
