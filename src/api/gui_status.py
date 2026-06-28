"""
/api/gui_status payload — read-only GUI polling contract.

Lifecycle and observability only; does not touch trading logic.
"""

from __future__ import annotations

import time
from typing import Any

from runtime.app_mode import resolve_app_mode, resolve_data_root
from runtime.pipeline_governance import build_pipeline_governance
from runtime.pipeline_health import (
    build_api_feed_health,
    build_market_rotation_status,
    build_trade_pipeline_health,
)
from runtime.session_identity import build_session_identity_fields
from runtime.session_lock import lock_path_for_scope, read_session_lock, resolve_account_scope
from runtime.session_review import build_session_review_bundle
from runtime.strategy_controller import build_strategy_controller_decisions
from runtime.adaptive_thresholds import build_adaptive_thresholds
from runtime.strategy_governance import build_strategy_governance
from runtime.unified_execution import build_unified_execution_routes
from runtime.daily_pnl_targeting import build_daily_pnl_targeting
from runtime.regime_sizing import build_regime_sizing_advice
from runtime.regime_risk_envelope import build_regime_risk_envelope
from runtime.regime_aware_selector import build_regime_aware_strategy_selector
from runtime.regime_detection import build_regime_detection_bundle
from runtime.strategy_performance_memory import build_strategy_performance_bundle
from runtime.hard_enforcement import build_hard_enforcement_decisions
from runtime.strategy_enforcement import build_strategy_enforcement_decisions
from runtime.strategy_selector import build_strategy_selector_advice
from runtime.strategy_transition import build_strategy_transition_advice


def _session_lock_record() -> dict[str, Any] | None:
    try:
        mode = resolve_app_mode()
        scope = resolve_account_scope(mode)
        root = resolve_data_root(mode)
        path = lock_path_for_scope(scope, root)
        return read_session_lock(path)
    except Exception:
        return None


def _session_uptime_sec(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    started = record.get("started_at")
    if started is None:
        return None
    try:
        return max(0.0, time.time() - float(started))
    except (TypeError, ValueError):
        return None


def _engine_health(paths_armed: dict[str, bool]) -> dict[str, str]:
    def _label(key: str) -> str:
        return "armed" if paths_armed.get(key) else "unarmed"

    return {
        "path_a": _label("path_a"),
        "path_b": _label("path_b"),
        "micro": _label("micro"),
    }


def _last_trade_timestamp() -> str | None:
    try:
        from api.v31_telemetry import build_v31_history

        hist = build_v31_history(limit=1)
        rows = hist.get("rows") or []
        if not rows:
            return None
        row = rows[0]
        ts = row.get("closed_at") or row.get("executed_at")
        return str(ts) if ts else None
    except Exception:
        return None


def warm_unified_execution_route_cache() -> int:
    """Boot-time P0: populate unified route cache without /api/gui_status poll.

    Runs the full advisory chain through ``build_unified_execution_routes`` so
    execution guards are active before the first dispatch. Fail-open on error.
    """
    try:
        payload = build_gui_status()
        routes = payload.get("unified_execution_route") or []
        return len(routes)
    except Exception:
        return 0


def build_gui_status() -> dict[str, Any]:
    """Merge session identity + GUI pipeline health for /api/gui_status."""
    identity = build_session_identity_fields()
    record = _session_lock_record()
    paths = identity.get("engine_paths_armed") or identity.get("paths_armed") or {}
    session_status = str(identity.get("session_status") or "").upper()
    lock_present = record is not None
    feed_health = build_api_feed_health()
    pipeline_rows = build_trade_pipeline_health()
    rotation_status = build_market_rotation_status()
    governance = build_pipeline_governance(
        trade_pipeline_health=pipeline_rows,
        api_feed_health=feed_health,
        market_rotation_status=rotation_status,
    )
    selector_advice = build_strategy_selector_advice(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
        market_rotation_status=rotation_status,
        session_governance=governance.get("session_governance") or {},
    )
    controller_decisions = build_strategy_controller_decisions(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        strategy_selector_advice=selector_advice,
    )
    transition_advice = build_strategy_transition_advice(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
        market_rotation_status=rotation_status,
        session_governance=governance.get("session_governance") or {},
        strategy_selector_advice=selector_advice,
    )
    enforcement_decisions = build_strategy_enforcement_decisions(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
        strategy_controller_decisions=controller_decisions,
        strategy_transition_advice=transition_advice,
        strategy_selector_advice=selector_advice,
    )
    hard_enforcement_decisions = build_hard_enforcement_decisions(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
        strategy_controller_decisions=controller_decisions,
        strategy_transition_advice=transition_advice,
        strategy_selector_advice=selector_advice,
    )
    session_uptime = _session_uptime_sec(record)
    session_review_bundle = build_session_review_bundle(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        session_governance=governance.get("session_governance") or {},
        api_feed_health=feed_health,
        market_rotation_status=rotation_status,
        strategy_selector_advice=selector_advice,
        strategy_transition_advice=transition_advice,
        strategy_controller_decisions=controller_decisions,
        strategy_enforcement_decisions=enforcement_decisions,
        session_uptime_sec=session_uptime,
    )
    adaptive_thresholds = build_adaptive_thresholds(
        session_review=session_review_bundle.get("session_review"),
        loosening_advice=session_review_bundle.get("loosening_advice"),
        self_reflection=session_review_bundle.get("self_reflection"),
        strategy_selector_advice=selector_advice,
        strategy_transition_advice=transition_advice,
        strategy_controller_decisions=controller_decisions,
        hard_enforcement_decisions=hard_enforcement_decisions,
    )
    performance_bundle = build_strategy_performance_bundle(
        session_review=session_review_bundle.get("session_review"),
        self_reflection=session_review_bundle.get("self_reflection"),
        strategy_selector_advice=selector_advice,
        strategy_transition_advice=transition_advice,
        hard_enforcement_decisions=hard_enforcement_decisions,
        api_feed_health=feed_health,
        trade_pipeline_health=pipeline_rows,
    )
    regime_bundle = build_regime_detection_bundle(
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
        market_rotation_status=rotation_status,
        strategy_selector_advice=selector_advice,
        strategy_weighting_advice=performance_bundle.get("strategy_weighting_advice"),
    )
    regime_aware_strategy_selector = build_regime_aware_strategy_selector(
        regime_detection=regime_bundle.get("regime_detection"),
        regime_strategy_alignment=regime_bundle.get("regime_strategy_alignment"),
        strategy_performance_memory=performance_bundle.get("strategy_performance_memory"),
        strategy_weighting_advice=performance_bundle.get("strategy_weighting_advice"),
        adaptive_thresholds=adaptive_thresholds,
        session_review=session_review_bundle.get("session_review"),
        strategy_selector_advice=selector_advice,
        strategy_transition_advice=transition_advice,
        hard_enforcement_decisions=hard_enforcement_decisions,
        trade_pipeline_health=pipeline_rows,
    )
    regime_risk_envelope = build_regime_risk_envelope(
        regime_detection=regime_bundle.get("regime_detection"),
        regime_strategy_alignment=regime_bundle.get("regime_strategy_alignment"),
        strategy_performance_memory=performance_bundle.get("strategy_performance_memory"),
        strategy_weighting_advice=performance_bundle.get("strategy_weighting_advice"),
        adaptive_thresholds=adaptive_thresholds,
        session_review=session_review_bundle.get("session_review"),
        regime_aware_strategy_selector=regime_aware_strategy_selector,
        hard_enforcement_decisions=hard_enforcement_decisions,
        trade_pipeline_health=pipeline_rows,
    )
    regime_sizing_advice = build_regime_sizing_advice(
        regime_detection=regime_bundle.get("regime_detection"),
        regime_strategy_alignment=regime_bundle.get("regime_strategy_alignment"),
        regime_risk_envelope=regime_risk_envelope,
        strategy_performance_memory=performance_bundle.get("strategy_performance_memory"),
        strategy_weighting_advice=performance_bundle.get("strategy_weighting_advice"),
        adaptive_thresholds=adaptive_thresholds,
        session_review=session_review_bundle.get("session_review"),
        regime_aware_strategy_selector=regime_aware_strategy_selector,
        hard_enforcement_decisions=hard_enforcement_decisions,
        trade_pipeline_health=pipeline_rows,
    )
    # daily_pnl_targeting must build before unified_execution_route (route consumes it)
    # and before strategy_governance (progress_ratio history). Response order places
    # strategy_governance immediately after unified_execution_route.
    daily_pnl_targeting = build_daily_pnl_targeting(
        session_review=session_review_bundle.get("session_review"),
        regime_aware_strategy_selector=regime_aware_strategy_selector,
        regime_risk_envelope=regime_risk_envelope,
        regime_sizing_advice=regime_sizing_advice,
        strategy_performance_memory=performance_bundle.get("strategy_performance_memory"),
        adaptive_thresholds=adaptive_thresholds,
        regime_detection=regime_bundle.get("regime_detection"),
        hard_enforcement_decisions=hard_enforcement_decisions,
    )
    unified_execution_route = build_unified_execution_routes(
        regime_detection=regime_bundle.get("regime_detection"),
        regime_aware_strategy_selector=regime_aware_strategy_selector,
        regime_risk_envelope=regime_risk_envelope,
        regime_sizing_advice=regime_sizing_advice,
        daily_pnl_targeting=daily_pnl_targeting,
        hard_enforcement_decisions=hard_enforcement_decisions,
        soft_enforcement_decisions=enforcement_decisions,
        strategy_transition_advice=transition_advice,
        strategy_controller_decisions=controller_decisions,
        trade_pipeline_health=pipeline_rows,
        pipeline_governance=governance.get("pipeline_governance") or {},
        api_feed_health=feed_health,
    )
    strategy_governance = build_strategy_governance(
        strategy_performance_memory=performance_bundle.get("strategy_performance_memory"),
        adaptive_thresholds=adaptive_thresholds,
        regime_detection=regime_bundle.get("regime_detection"),
        regime_aware_strategy_selector=regime_aware_strategy_selector,
        regime_risk_envelope=regime_risk_envelope,
        regime_sizing_advice=regime_sizing_advice,
        daily_pnl_targeting=daily_pnl_targeting,
        session_review=session_review_bundle.get("session_review"),
        hard_enforcement_decisions=hard_enforcement_decisions,
    )

    return {
        **identity,
        "gui_attach_ready": lock_present and session_status == "HEALTHY" and bool(identity.get("session_id")),
        "engine_health": _engine_health(paths),
        "api_feed_health": feed_health,
        "trade_pipeline_health": pipeline_rows,
        "session_uptime": session_uptime,
        "last_trade_timestamp": _last_trade_timestamp(),
        "market_rotation_status": rotation_status,
        **governance,
        "strategy_selector_advice": selector_advice,
        "strategy_controller_decisions": controller_decisions,
        "strategy_transition_advice": transition_advice,
        "strategy_enforcement_decisions": enforcement_decisions,
        "hard_enforcement_decisions": hard_enforcement_decisions,
        "adaptive_thresholds": adaptive_thresholds,
        **performance_bundle,
        **regime_bundle,
        "regime_aware_strategy_selector": regime_aware_strategy_selector,
        "regime_risk_envelope": regime_risk_envelope,
        "regime_sizing_advice": regime_sizing_advice,
        "daily_pnl_targeting": daily_pnl_targeting,
        "unified_execution_route": unified_execution_route,
        "strategy_governance": strategy_governance,
        **session_review_bundle,
    }
