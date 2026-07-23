"""Non-blocking services started once Gate 5 marks the agent ACTIVE."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from system.boot.context import BootContext
from system.engine_log import log_engine

_SESSION_REFRESH_INTERVAL_SEC = 45 * 60
_boot_rest_client: Any | None = None


def get_boot_rest_client() -> Any | None:
    return _boot_rest_client


def _log_step_outcome(label: str, started: float, *, error: Exception | None = None) -> None:
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if error is None:
        log_engine(f"post-ready: {label} ok ({elapsed_ms:.0f}ms)")
    else:
        log_engine(
            f"post-ready: {label} failed ({elapsed_ms:.0f}ms) "
            f"{type(error).__name__}: {error}"
        )


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


def _start_tradeability_watchdog(rest: Any, cfg: Any) -> None:
    """24/7 — refresh active stack when IG market_status changes (EDITS_ONLY rollover)."""

    def _loop() -> None:
        while True:
            time.sleep(60.0)
            try:
                from runtime.dual_core_execution import refresh_active_stack_tradeability

                if refresh_active_stack_tradeability(cfg=cfg, rest=rest):
                    log_engine("tradeability_watchdog: active stack refreshed")
            except Exception as exc:
                log_engine(
                    f"tradeability_watchdog: {type(exc).__name__}: {exc}"
                )

    threading.Thread(target=_loop, name="tradeability-watchdog", daemon=True).start()
    log_engine("post-ready: tradeability watchdog started (60s)")


def _ensure_feed_plane_ready(rest: Any, cfg: Any) -> None:
    """P3 — feeds → fulfillment SHM → guardian before stacked tracks.

    Rotation bootstrap runs inside ``start_stacked_dual_asset_tracks`` so this
    path stays non-blocking (no synchronous Yahoo/REST universe scan here).
    """
    started = time.perf_counter()
    try:
        from runtime.dual_core_execution import ROTATION_UNIVERSE
        from system.feeds.data_feed_orchestrator import ensure_data_feed_orchestrator_running

        epics = list(ROTATION_UNIVERSE)
        ensure_data_feed_orchestrator_running(epics, cfg=cfg)
        _log_step_outcome(
            f"Data feed orchestrator armed ({len(epics)} epics, Yahoo primary)",
            started,
        )
    except Exception as e:
        _log_step_outcome("Data feed orchestrator", started, error=e)

    started = time.perf_counter()
    try:
        from system.unified_fulfillment_cache import start_fulfillment_cache_refresh

        start_fulfillment_cache_refresh()
        _log_step_outcome("Fulfillment cache refresh started (background SHM)", started)
    except Exception as e:
        _log_step_outcome("Fulfillment cache refresh", started, error=e)

    started = time.perf_counter()
    try:
        from system.cockpit_feed_guardian_agent import start_agent_feed_guardian

        start_agent_feed_guardian()
        _log_step_outcome("Agent feed guardian started", started)
    except Exception as e:
        _log_step_outcome("Agent feed guardian", started, error=e)


def materialize_post_g5_execution_plane(context: BootContext) -> None:
    """Arm stacked sweep + execution watchdogs — isolated from coordinator failures."""
    rest = context.rest_client
    cfg = context.config
    if rest is None:
        log_engine("post-ready: execution plane skipped — no rest_client on BootContext")
        return

    try:
        from cockpit.emergency import clear_emergency_cockpit_override

        clear_emergency_cockpit_override(resume_trading=True)
        log_engine("post-ready: COCKPIT_EMERGENCY_OVERRIDE purged — Gate 5 valve open")
    except Exception as e:
        log_engine(f"post-ready: cockpit override purge skipped: {type(e).__name__}: {e}")

    from runtime.dual_core_execution import (
        lock_forex_rotation_session,
        start_stacked_dual_asset_tracks,
    )

    dual_cfg = (cfg.get("dual_core") or {}) if cfg is not None else {}
    if dual_cfg.get("forex_rotation_locked"):
        try:
            lock_forex_rotation_session(
                reason=str(dual_cfg.get("lock_reason") or "config_forex_rotation_locked"),
                cfg=cfg,
                rest=rest,
            )
            log_engine(
                "post-ready: ForexRotationLock EUR/USD + GBP/USD "
                "(indices/metals dropped from hot path)"
            )
        except Exception as e:
            log_engine(f"post-ready: forex rotation lock skipped: {type(e).__name__}: {e}")

    try:
        from data.learning_store import LearningStore
        from runtime.live_canary_session import reset_live_canary_session_gates

        _lc_store = LearningStore(str(getattr(cfg, "learning_db", "")))
        reset_live_canary_session_gates(_lc_store, cfg=cfg)
    except Exception as e:
        log_engine(
            f"post-ready: live_canary session reset skipped: {type(e).__name__}: {e}"
        )

    _ensure_feed_plane_ready(rest, cfg)

    try:
        from system.agent_execution_mode import production_execution_active
        from feeder.mock_feed_engine import start_aggressive_momentum_wave

        if not production_execution_active():
            start_aggressive_momentum_wave()
            log_engine("post-ready: mock momentum wave armed (deferred from Gate1)")
    except Exception as e:
        log_engine(
            f"post-ready: mock momentum wave skipped: {type(e).__name__}: {e}"
        )

    try:
        from execution.correlation_guard import reset_session

        reset_session()
        log_engine("post-ready: correlation guard session reset")
    except Exception as e:
        log_engine(
            f"post-ready: correlation guard reset skipped: {type(e).__name__}: {e}"
        )
    try:
        from system.demo_execution_plane import arm_demo_unlimited_trading_session

        arm_demo_unlimited_trading_session(clear_counts=True)
    except Exception as e:
        log_engine(
            f"post-ready: demo unlimited trading arm skipped: {type(e).__name__}: {e}"
        )
    try:
        from runtime.broker_reject_guard import reset_broker_reject_guard_for_tests

        reset_broker_reject_guard_for_tests()
        log_engine("post-ready: broker reject guard cleared for fresh session")
    except Exception as e:
        log_engine(
            f"post-ready: broker reject guard reset skipped: {type(e).__name__}: {e}"
        )

    # Arm open-book supervision BEFORE any REST-heavy reconcile. trade_support
    # (and other launchd helpers) can saturate the shared REST budget during
    # Gate-2 hydrate; a blocking reconcile here previously stalled the plane
    # forever so OpenPositionManager never started.
    try:
        from runtime.strategy_kill_switch import clear_strategy_kill_switch

        clear_strategy_kill_switch()
        log_engine("post-ready: strategy kill-switch cleared for fresh session")
    except Exception as e:
        log_engine(
            f"post-ready: strategy kill-switch clear skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.dual_core_execution import start_stacked_dual_asset_tracks

        start_stacked_dual_asset_tracks(cfg=cfg, force=True)
        log_engine("post-ready: StackedDualAsset parallel tracks armed")
    except Exception as e:
        log_engine(
            f"post-ready: stacked dual-asset tracks failed: {type(e).__name__}: {e}"
        )

    try:
        from runtime.dual_core_execution import start_socket_heartbeat_validator

        start_socket_heartbeat_validator(interval_sec=1.0)
        log_engine("post-ready: SocketHeartbeat validator armed (5s stale → rehydrate)")
    except Exception as e:
        log_engine(f"post-ready: socket heartbeat skipped: {type(e).__name__}: {e}")

    try:
        from runtime.virtual_stop_loss import start_virtual_stop_watchdog

        start_virtual_stop_watchdog(rest)
        log_engine("post-ready: VirtualStop 2.0pt watchdog armed (500ms)")
    except Exception as e:
        log_engine(f"post-ready: virtual stop watchdog skipped: {type(e).__name__}: {e}")

    try:
        from runtime.dynamic_limit_engine import start_dynamic_limit_engine

        start_dynamic_limit_engine()
        log_engine("post-ready: dynamic limit engine armed")
    except Exception as e:
        log_engine(f"post-ready: dynamic limit engine skipped: {type(e).__name__}: {e}")

    try:
        from runtime.micro_gbp_exit import start_micro_gbp_exit_engine

        start_micro_gbp_exit_engine(rest)
        log_engine("post-ready: MicroGbpExit engine started (hydrate deferred)")
    except Exception as e:
        log_engine(f"post-ready: micro gbp exit skipped: {type(e).__name__}: {e}")

    try:
        from runtime.open_position_manager import start_open_position_manager

        start_open_position_manager(rest, cfg=cfg)
        log_engine("post-ready: OpenPositionManager supervisor armed")
    except Exception as e:
        log_engine(f"post-ready: open position manager skipped: {type(e).__name__}: {e}")

    try:
        from runtime.feed_health_watchdog import start_feed_health_watchdog

        start_feed_health_watchdog()
        log_engine("post-ready: FeedHealthWatchdog armed (5s stale → block+flatten)")
    except Exception as e:
        log_engine(f"post-ready: feed health watchdog skipped: {type(e).__name__}: {e}")

    def _background_broker_hydrate() -> None:
        """REST reconcile/hydrate off the post-ready critical path."""
        if rest is None or cfg is None:
            return
        try:
            from data.learning_store import LearningStore
            from runtime.active_lifecycle_trades import boot_reconcile_active_trades

            store = LearningStore(str(cfg.learning_db))
            counts = boot_reconcile_active_trades(rest, store)
            log_engine(
                "post-ready: active lifecycle boot reconcile "
                f"adopted={counts.get('adopted', 0)} "
                f"closed_registry={counts.get('closed_registry', 0)} "
                f"synced={counts.get('synced', 0)}"
            )
        except Exception as e:
            log_engine(
                f"post-ready: active lifecycle boot reconcile skipped: "
                f"{type(e).__name__}: {e}"
            )
        try:
            from runtime.micro_gbp_exit import hydrate_open_positions_from_broker

            hydrate_open_positions_from_broker(rest, cfg=cfg)
            log_engine("post-ready: MicroGbpExit broker hydrate complete")
        except Exception as e:
            log_engine(
                f"post-ready: MicroGbpExit hydrate skipped: {type(e).__name__}: {e}"
            )
        # Adopt every broker open into the software risk stack + RAM matrix so a
        # mid-session bytecode recycle never leaves inflight trades unmonitored.
        try:
            from execution.position_risk_stack import (
                ensure_risk_stack_coverage,
                reconcile_open_positions_risk_stack,
            )
            from system.memory_context import get_memory_context

            ensure_risk_stack_coverage(rest, cfg=cfg, force=True)
            stack = reconcile_open_positions_risk_stack(rest, cfg=cfg, force=True)
            items = list(rest.open_positions(budget_priority=True) or [])
            rows = []
            for it in items:
                pos = it.get("position") or {}
                mkt = it.get("market") or {}
                rows.append(
                    {
                        "deal_id": str(pos.get("dealId") or ""),
                        "epic": str(mkt.get("epic") or ""),
                        "direction": str(pos.get("direction") or "BUY"),
                        "size": float(pos.get("size") or 0),
                        "entry": float(pos.get("level") or 0),
                        "pnl_gbp": pos.get("upl") or pos.get("unrealised") or 0.0,
                        "source": "boot_inflight_adopt",
                    }
                )
            verified = get_memory_context().sync_open_rows(rows)
            log_engine(
                "post-ready: inflight adopt "
                f"broker={len(items)} memory={len(verified)} "
                f"stack_armed={stack.get('armed', 0)} gbp={stack.get('gbp', 0)}"
            )
        except Exception as e:
            log_engine(
                f"post-ready: inflight adopt skipped: {type(e).__name__}: {e}"
            )

    threading.Thread(
        target=_background_broker_hydrate,
        name="post-ready-broker-hydrate",
        daemon=True,
    ).start()
    log_engine("post-ready: broker lifecycle reconcile scheduled (background)")

    try:
        from runtime.deploy_hold import warn_if_deploy_window_closed

        warn_if_deploy_window_closed(rest, cfg=cfg)
    except Exception as e:
        log_engine(f"post-ready: deploy_hold check skipped: {type(e).__name__}: {e}")

    try:
        from runtime.trading_desk_liveness import start_trading_desk_liveness_monitor

        start_trading_desk_liveness_monitor()
        log_engine("post-ready: TradingDeskLiveness monitor armed")
    except Exception as e:
        log_engine(f"post-ready: trading desk liveness skipped: {type(e).__name__}: {e}")

    try:
        if not _harness_mode():
            from runtime.boot_sot_fallback import arm_boot_fallback_circuit
            from runtime.desk_stability_harness import (
                note_boot_started,
                start_desk_stability_harness,
            )

            note_boot_started()
            arm_boot_fallback_circuit(reason="post_ready")
            start_desk_stability_harness(cfg)
            log_engine("post-ready: DeskStability harness armed (PERF/background)")
        else:
            log_engine("post-ready: DeskStability harness skipped (IG_TEST_HARNESS)")
    except Exception as e:
        log_engine(f"post-ready: DeskStability harness skipped: {type(e).__name__}: {e}")

    try:
        from runtime.strategy_improvement_tracker import load_persisted_state

        load_persisted_state()
        log_engine("post-ready: strategy improvement tracker loaded")
    except Exception as e:
        log_engine(f"post-ready: strategy improvement skipped: {type(e).__name__}: {e}")

    try:
        from runtime.intraday_slot_tracker import load_persisted_state as load_intraday_slots

        load_intraday_slots()
        log_engine("post-ready: intraday slot tracker loaded")
    except Exception as e:
        log_engine(f"post-ready: intraday slot tracker skipped: {type(e).__name__}: {e}")

    try:
        from system.unified_runtime_state import init_unified_runtime_state, update_stops_limits

        init_unified_runtime_state()
        update_stops_limits(trailing_active=True, dynamic_limit_active=True)
    except Exception as e:
        log_engine(
            f"post-ready: unified runtime state hook skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.ledger_hydration_core import bootstrap_ledger_history_once

        threading.Thread(
            target=bootstrap_ledger_history_once,
            args=(rest,),
            name="ledger-hydration-bootstrap",
            daemon=True,
        ).start()
        log_engine("post-ready: LedgerHydration one-time IG history sync armed")
    except Exception as e:
        log_engine(
            f"post-ready: ledger hydration bootstrap skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.trade_manager import start_dual_core_coordinator

        start_dual_core_coordinator(rest, config=cfg)
        log_engine("post-ready: DualCoreCoordinator ENGINE_B_MICRO_SCALPER armed")
        try:
            from runtime.dual_core_execution import start_micro_scalper_tick_lane

            if start_micro_scalper_tick_lane():
                log_engine("post-ready: MicroScalper instant tick lane armed")
        except Exception as e:
            log_engine(
                f"post-ready: micro scalper tick lane skipped: {type(e).__name__}: {e}"
            )
        try:
            from runtime.session_trade_unlimited import inject_session_unlimited_trades

            inject_session_unlimited_trades()
            log_engine("post-ready: session trade caps and order cadence unlimited")
        except Exception as e:
            log_engine(
                f"post-ready: session unlimited trades inject skipped: "
                f"{type(e).__name__}: {e}"
            )
    except Exception as e:
        log_engine(
            f"post-ready: dual-core coordinator skipped: {type(e).__name__}: {e}"
        )


def start_post_ready_services(context: BootContext) -> None:
    """Start schedulers and monitors after dormant loops are unpaused."""
    global _boot_rest_client
    _boot_rest_client = context.rest_client
    try:
        from system.boot.boot_orchestrator import (
            BootStage,
            SubsystemId,
            mark_stage_running,
            mark_subsystem,
            record_boot_event,
        )
        from system.boot.boot_orchestrator import StepStatus
        from system.boot.iron_gauge import (
            GaugePhase,
            PhaseStatus,
            enforce_post_ready_order,
            iron_gauge_mark,
        )

        mark_stage_running(BootStage.B)
        record_boot_event("post_ready_begin", stage=BootStage.B.value)
    except Exception:
        pass
    if _harness_mode():
        log_engine("post-ready: harness fast-path — skipping non-essential daemons")
        return

    cfg = context.config
    rest = context.rest_client

    # Eager route warm before execution plane — clears routing_unarmed at iron cage.
    if rest is not None:
        try:
            from api.gui_status import warm_unified_execution_route_cache

            route_count = warm_unified_execution_route_cache()
            log_engine(
                f"post-ready: unified execution route cache warmed ({route_count} route(s))"
            )
        except Exception as exc:
            log_engine(
                f"post-ready: unified route cache warm-up skipped: "
                f"{type(exc).__name__}: {exc}"
            )

    # Arm stacked sweep + feed plane first — launcher Stage 6 polls health_light within seconds.
    if rest is not None:
        try:
            from system.boot.iron_gauge import GaugePhase, PhaseStatus, enforce_post_ready_order, iron_gauge_mark

            enforce_post_ready_order(GaugePhase.POST_EXECUTION_PLANE)
            iron_gauge_mark(GaugePhase.POST_EXECUTION_PLANE, PhaseStatus.RUNNING)
        except Exception:
            pass
        materialize_post_g5_execution_plane(context)
        try:
            from system.boot.iron_gauge import GaugePhase, PhaseStatus, iron_gauge_mark

            iron_gauge_mark(GaugePhase.POST_EXECUTION_PLANE, PhaseStatus.OK)
        except Exception:
            pass
    try:
        from api.health_light import start_health_light_refresher

        start_health_light_refresher()
        log_engine("post-ready: HealthLight 1s refresher started (early)")
        try:
            from system.boot.iron_gauge import GaugePhase, PhaseStatus, enforce_post_ready_order, iron_gauge_mark

            enforce_post_ready_order(GaugePhase.POST_HEALTH_LIGHT)
            iron_gauge_mark(GaugePhase.POST_HEALTH_LIGHT, PhaseStatus.OK)
        except Exception:
            pass
    except Exception as e:
        log_engine(f"post-ready: health_light refresher skipped: {type(e).__name__}: {e}")

    # Critical path — GUI reads iron-ledger orchestrator telemetry; must not wait
    # behind KernelInterceptor's O(n) trading/execution module walk.
    try:
        from runtime.master_orchestrator import start_master_orchestrator
        from system.boot.iron_gauge import GaugePhase, PhaseStatus, enforce_post_ready_order, iron_gauge_mark

        enforce_post_ready_order(GaugePhase.POST_ORCHESTRATOR)
        iron_gauge_mark(GaugePhase.POST_ORCHESTRATOR, PhaseStatus.RUNNING)
        warm = start_master_orchestrator(rest=rest)
        log_engine(
            f"post-ready: master_orchestrator armed early primed={warm.get('primed')} "
            f"async={warm.get('async_warmup')}"
        )
        iron_gauge_mark(
            GaugePhase.POST_ORCHESTRATOR,
            PhaseStatus.OK,
            detail=f"primed={warm.get('primed')}",
        )
    except Exception as e:
        log_engine(
            f"post-ready: master_orchestrator early arm skipped: {type(e).__name__}: {e}"
        )

    def _install_kernel_interceptor_background() -> None:
        try:
            from system.guard.kernel_interceptor import install_kernel_interceptor
            from system.boot.iron_gauge import GaugePhase, PhaseStatus, iron_gauge_mark

            summary = install_kernel_interceptor()
            log_engine(
                "post-ready: KernelInterceptor armed (background) "
                f"(trading_wrapped={summary.get('trading_wrapped')} "
                f"execution_wrapped={summary.get('execution_wrapped')})"
            )
            iron_gauge_mark(GaugePhase.POST_KERNEL, PhaseStatus.OK, detail="kernel_armed")
        except Exception as exc:
            log_engine(
                f"post-ready: KernelInterceptor deferred — {type(exc).__name__}: {exc}"
            )
            try:
                from system.boot.iron_gauge import GaugePhase, PhaseStatus, iron_gauge_mark

                iron_gauge_mark(
                    GaugePhase.POST_KERNEL,
                    PhaseStatus.DEGRADED,
                    detail=str(exc)[:120],
                )
            except Exception:
                pass

    threading.Thread(
        target=_install_kernel_interceptor_background,
        name="post-ready-kernel-interceptor",
        daemon=True,
    ).start()
    log_engine("post-ready: KernelInterceptor install scheduled (background)")
    try:
        from system.boot.iron_gauge import GaugePhase, PhaseStatus, enforce_post_ready_order, iron_gauge_mark

        enforce_post_ready_order(GaugePhase.POST_KERNEL)
        iron_gauge_mark(GaugePhase.POST_KERNEL, PhaseStatus.RUNNING, detail="background_scheduled")
    except Exception:
        pass

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
        from alpha.geopolitical_monitor import start_geopolitical_monitor

        start_geopolitical_monitor()
        log_engine("post-ready: geopolitical oil/VIX monitor started")
    except Exception as e:
        log_engine(f"post-ready: geopolitical monitor skipped: {type(e).__name__}: {e}")

    try:
        from diagnostics.performance_journal import start_performance_journal

        start_performance_journal()
        log_engine("post-ready: performance journal armed")
    except Exception as e:
        log_engine(f"post-ready: performance journal skipped: {type(e).__name__}: {e}")

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
    if rest is not None:
        _start_tradeability_watchdog(rest, cfg)

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

    try:
        from analytics.multimarket_eval import start_multimarket_eval_refresher
        from analytics.trade_quality import start_trade_quality_refresher

        start_multimarket_eval_refresher()
        start_trade_quality_refresher()
        log_engine("post-ready: multimarket_eval + trade_quality refreshers started")
    except Exception as e:
        log_engine(
            f"post-ready: analytics refreshers skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.regime_switch_engine import start_regime_switch_refresher
        from system.volatility_risk_engine import start_volatility_risk_refresher
        from system.broker_reconciliation_daemon import start_broker_reconciliation_daemon

        start_regime_switch_refresher()
        _risk_store = None
        try:
            from data.learning_store import LearningStore

            if cfg is not None:
                _risk_store = LearningStore(str(cfg.learning_db))
        except Exception:
            pass
        start_volatility_risk_refresher(store=_risk_store)
        start_broker_reconciliation_daemon(rest=rest)
        log_engine("post-ready: regime + vol-risk + reconciliation daemons started")
    except Exception as e:
        log_engine(
            f"post-ready: regime/risk/reconcile skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.parameter_tuner import start_parameter_tuner_daemon

        start_parameter_tuner_daemon()
        log_engine("post-ready: parameter_tuner daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: parameter_tuner skipped: {type(e).__name__}: {e}"
        )

    try:
        from runtime.portfolio_exploration_engine import start_portfolio_exploration_daemon

        start_portfolio_exploration_daemon()
        log_engine("post-ready: portfolio_exploration daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: portfolio_exploration skipped: {type(e).__name__}: {e}"
        )

    try:
        from system.chaos_guardian import start_chaos_guardian

        start_chaos_guardian(rest=rest)
        log_engine("post-ready: chaos_guardian daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: chaos_guardian skipped: {type(e).__name__}: {e}"
        )

    try:
        from system.autonomic_healer import start_autonomic_healer

        start_autonomic_healer(rest=rest)
        log_engine("post-ready: autonomic_healer daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: autonomic_healer skipped: {type(e).__name__}: {e}"
        )

    try:
        from system.alert_reporting_matrix import start_alert_reporting_matrix

        start_alert_reporting_matrix()
        log_engine("post-ready: alert_reporting_matrix started")
    except Exception as e:
        log_engine(
            f"post-ready: alert_reporting skipped: {type(e).__name__}: {e}"
        )

    try:
        from system.backup_manager import start_backup_daemon

        start_backup_daemon()
        log_engine("post-ready: database backup daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: backup_manager skipped: {type(e).__name__}: {e}"
        )

    # PERF / background plane — never on the 0.0ms tick lane
    try:
        from runtime.log_rotator_daemon import start_log_rotator_daemon

        start_log_rotator_daemon()
        log_engine("post-ready: PERF log_rotator_daemon started")
    except Exception as e:
        log_engine(
            f"post-ready: log_rotator_daemon skipped: {type(e).__name__}: {e}"
        )

    try:
        from analytics.eod_settlement_reporter import start_eod_settlement_reporter

        start_eod_settlement_reporter()
        log_engine("post-ready: PERF eod_settlement_reporter started")
    except Exception as e:
        log_engine(
            f"post-ready: eod_settlement_reporter skipped: {type(e).__name__}: {e}"
        )

    try:
        from analytics.weekly_performance_ledger import start_weekly_performance_ledger

        start_weekly_performance_ledger()
        log_engine("post-ready: PERF weekly_performance_ledger started")
    except Exception as e:
        log_engine(
            f"post-ready: weekly_performance_ledger skipped: {type(e).__name__}: {e}"
        )

    if rest is not None:
        try:
            from runtime.trade_manager import start_dual_core_coordinator

            start_dual_core_coordinator(rest, config=cfg)
            log_engine("post-ready: DualCoreCoordinator ENGINE_B_MICRO_SCALPER armed")
            try:
                from runtime.dual_core_execution import start_micro_scalper_tick_lane

                if start_micro_scalper_tick_lane():
                    log_engine("post-ready: MicroScalper instant tick lane armed")
            except Exception as e:
                log_engine(
                    f"post-ready: micro scalper tick lane skipped: {type(e).__name__}: {e}"
                )
            try:
                from runtime.session_trade_unlimited import inject_session_unlimited_trades

                inject_session_unlimited_trades()
                log_engine("post-ready: session trade caps and order cadence unlimited")
            except Exception as e:
                log_engine(
                    f"post-ready: session unlimited trades inject skipped: "
                    f"{type(e).__name__}: {e}"
                )
        except Exception as e:
            log_engine(
                f"post-ready: dual-core coordinator skipped: {type(e).__name__}: {e}"
            )

    _sync_loops_accepting_ticks_from_plane()
    _schedule_accepting_ticks_sync_retries()

    try:
        from system.agent_orchestration import maybe_start_agent_orchestrator

        if maybe_start_agent_orchestrator():
            log_engine("post-ready: v33 agent orchestrator self-heal daemon armed")
    except Exception as e:
        log_engine(
            f"post-ready: agent orchestrator skipped: {type(e).__name__}: {e}"
        )

    try:
        from system.boot.iron_gauge import GaugePhase, PhaseStatus, iron_gauge_mark

        iron_gauge_mark(GaugePhase.POST_TAIL, PhaseStatus.OK, detail="post_ready_services_complete")
    except Exception:
        pass


def _schedule_accepting_ticks_sync_retries() -> None:
    """Exec loop may arm seconds after post_ready — retry sync without blocking boot."""

    def _retry() -> None:
        import time

        for _ in range(18):
            time.sleep(5.0)
            _sync_loops_accepting_ticks_from_plane()

    threading.Thread(
        target=_retry,
        name="accepting-ticks-sync",
        daemon=True,
    ).start()


def _sync_loops_accepting_ticks_from_plane() -> None:
    """Mirror health_light execution plane into SystemState.loops.accepting_ticks."""
    try:
        from api.health_light import get_health_light_response
        from system.system_state import BootPhase, get_system_state

        hl = get_health_light_response()
        exec_active = bool(hl.get("execution_loop_active"))
        armed = int((hl.get("routing_state") or {}).get("armed") or 0)
        stacked = bool(hl.get("stacked_sweep_alive"))
        hub = (hl.get("data_feeds") or {}).get("hub") or {}
        fresh = int(hub.get("fresh_count") or 0)
        if not (exec_active and armed > 0 and (stacked or fresh >= 1)):
            return

        state = get_system_state()
        snap = state.try_snapshot(timeout=0.5)
        if snap is None:
            return
        loops = dict(snap.get("loops") or {})
        if loops.get("accepting_ticks"):
            return
        state.update_state(
            BootPhase.READY,
            int(snap.get("percent") or 100),
            str(snap.get("phase_label") or "ACTIVE"),
            loops={
                **loops,
                "built": max(int(loops.get("built") or 0), armed),
                "running": True,
                "accepting_ticks": True,
            },
        )
        log_engine("post-ready: loops.accepting_ticks synced from health_light plane")
    except Exception as e:
        log_engine(
            f"post-ready: accepting_ticks sync skipped: {type(e).__name__}: {e}"
        )
