"""
Runtime execution plane selector — SHADOW ledger vs authentic IG DEMO broker.

IG_AGENT_MODE values:
  SHADOW — simulated fills (shadow_ledger.jsonl), no IG orders
  DEMO   — LiveExecutor → IG DEMO REST (Gate 7)
  LIVE   — LiveExecutor → IG LIVE REST (blocked by demo_only_deployment)
"""

from __future__ import annotations

import os
from typing import Literal

AgentExecutionMode = Literal["SHADOW", "DEMO", "LIVE", ""]

_VALID = frozenset({"SHADOW", "DEMO", "LIVE"})


def agent_execution_mode() -> AgentExecutionMode:
    raw = os.environ.get("IG_AGENT_MODE", "").strip().upper()
    if raw in _VALID:
        return raw  # type: ignore[return-value]
    return ""


def shadow_execution_active() -> bool:
    """Route Gate 7 to ShadowExecutor (paper ledger only)."""
    return agent_execution_mode() == "SHADOW"


def demo_broker_execution_active() -> bool:
    """Route Gate 7 to LiveExecutor against IG DEMO REST."""
    return agent_execution_mode() == "DEMO"


def broker_rest_execution_active() -> bool:
    return agent_execution_mode() in ("DEMO", "LIVE")


def broker_demo_execution_required() -> bool:
    """Mock feed and ShadowExecutor must not intercept the path."""
    if mock_feed_explicitly_disabled():
        return True
    return demo_broker_execution_active()


def mock_feed_explicitly_disabled() -> bool:
    return production_execution_active() or os.environ.get("IG_MOCK_FEED", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def production_execution_active() -> bool:
    """Force authentic IGRestClient — no MockIGRest / MockFeedEngine sandbox."""
    return os.environ.get("IG_PRODUCTION_EXECUTION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def authentic_demo_broker_required() -> bool:
    """Real IGRestClient on demo-api.ig.com — MockIGRest / in-memory paper forbidden."""
    if production_execution_active():
        return True
    if not demo_broker_execution_active():
        return False
    return os.environ.get("IG_MOCK_FEED", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def ensure_demo_broker_execution_armed_on_boot() -> None:
    """Arm authentic IG DEMO REST — IGRestClient → demo-api.ig.com (not MockIGRest)."""
    if production_execution_active():
        return
    os.environ["IG_AGENT_MODE"] = "DEMO"
    os.environ["IG_PRODUCTION_EXECUTION"] = "0"
    os.environ["IG_MOCK_FEED"] = "0"
    os.environ.pop("IG_MOCK_FEED_ACTIVE", None)
    os.environ.pop("IG_ALLOW_MOCK_TRADING", None)
    try:
        from system.engine_log import log_engine

        log_engine(
            "IG DEMO broker armed — IGRestClient → demo-api.ig.com "
            "(IG_PRODUCTION_EXECUTION=0, live probe enabled)"
        )
    except Exception:
        pass


def ensure_production_execution_armed_on_boot() -> None:
    """Arm real IG broker path before gates — shatters mock sandbox cage."""
    if not production_execution_active():
        return
    os.environ["IG_AGENT_MODE"] = "DEMO"
    os.environ["IG_MOCK_FEED"] = "0"
    os.environ.pop("IG_MOCK_FEED_ACTIVE", None)
    os.environ.pop("IG_ALLOW_MOCK_TRADING", None)
    try:
        from system.engine_log import log_engine

        log_engine(
            "IG_PRODUCTION_EXECUTION=1 — MockIGRest forbidden; "
            "forcing IGRestClient DEMO broker tunnel"
        )
    except Exception:
        pass


def session_validation_active() -> bool:
    """Friday session-validation capture — lowered floors + execution unblock."""
    return os.environ.get("IG_SESSION_VALIDATION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_session_validation_armed = False
_demo_sandbox_armed = False


def demo_operational_floors_active() -> bool:
    """Lower confidence/ATR floors — IG_SESSION_VALIDATION=1 or IG_AGENT_MODE=DEMO."""
    return session_validation_active() or demo_broker_execution_active()


def demo_sandbox_unblock_active() -> bool:
    """Bypass weekend blackouts and master-kill hard-blocks for IG DEMO E2E."""
    return demo_broker_execution_active() or session_validation_active()


def force_market_open_active() -> bool:
    """24/7 execution plane — ignore real-world cash index weekend closures."""
    return session_validation_active() or demo_broker_execution_active()


def ensure_demo_sandbox_execution_armed() -> None:
    """Boot: clear ghost loss streaks and circuit-breaker pause for DEMO dispatch."""
    global _demo_sandbox_armed
    if _demo_sandbox_armed or not demo_broker_execution_active():
        return
    _demo_sandbox_armed = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "DEMO sandbox: clearing circuit breaker + weekend/kill-switch blocks "
            "— LiveExecutor IG REST armed (42% floor, ATR 0.0)"
        )
    except Exception:
        pass
    try:
        from data.learning_store import LearningStore

        store = LearningStore()
        store.reset_consecutive_loss_streak(reason="demo_sandbox_boot_reset")
        store.clear_circuit_breaker_state()
    except Exception:
        pass
    try:
        from system.qmm_process_supervisor import clear_process_entry_block

        clear_process_entry_block()
    except Exception:
        pass
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        ref = getattr(MarketOrchestrator, "_ORCHESTRATOR_REF", None)
        if ref is None:
            from runtime import market_orchestrator as mo

            ref = mo._ORCHESTRATOR_REF
        for loop in getattr(ref, "_loops", []) or []:
            fn = getattr(loop, "clear_entry_circuit_breaker", None)
            if callable(fn):
                fn()
    except Exception:
        pass


def ensure_session_validation_execution_armed() -> None:
    """Clear stale circuit-breaker / validation blocks for IG DEMO dispatch."""
    global _session_validation_armed
    if _session_validation_armed or not session_validation_active():
        return
    _session_validation_armed = True
    try:
        from system.engine_log import log_engine

        log_engine(
            "Session validation: clearing circuit breaker + ATR hold for IG DEMO dispatch"
        )
    except Exception:
        pass
    try:
        from data.learning_store import LearningStore

        store = LearningStore()
        if not store.has_strategy_trades_within_hours(24):
            store.reset_consecutive_loss_streak(
                reason="session_validation_24h_reset"
            )
            log_engine(
                "Session validation: no strategy trades in 24h — consecutive loss register reset to 0"
            )
        else:
            store.clear_circuit_breaker_state()
    except Exception:
        pass


def resolve_default_execution_mode_for_boot() -> None:
    """
    One-shot boot: honour explicit IG_AGENT_MODE; never force SHADOW.

    Apex desktop with IG_AGENT_MODE unset defaults to DEMO broker execution.
    """
    current = agent_execution_mode()
    if current:
        return
    if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
        os.environ["IG_AGENT_MODE"] = "DEMO"
        return
    if mock_feed_explicitly_disabled():
        os.environ["IG_AGENT_MODE"] = "DEMO"


def ensure_execution_mode_resolved_on_boot() -> None:
    """G1-safe: resolve IG_AGENT_MODE without opening LearningStore."""
    ensure_demo_broker_execution_armed_on_boot()
    ensure_production_execution_armed_on_boot()
    resolve_default_execution_mode_for_boot()


def ensure_learning_store_execution_armed() -> None:
    """Deferred past G3 — clears circuit-breaker state via LearningStore."""
    ensure_demo_sandbox_execution_armed()
    ensure_session_validation_execution_armed()


def schedule_post_g3_execution_arming() -> None:
    """Arm LearningStore-backed execution plane off the G2/G3 hydration thread."""
    import threading

    threading.Thread(
        target=ensure_learning_store_execution_armed,
        name="post-g3-exec-arm",
        daemon=True,
    ).start()


def ensure_execution_plane_armed_on_boot() -> None:
    """One-shot G1: mode resolution only — LearningStore deferred to post-G3."""
    ensure_execution_mode_resolved_on_boot()
