"""Fast gui_status slice — feeds, pipeline, governance shell (no regime DAG)."""

from __future__ import annotations

from typing import Any

from api.endpoint_profiler import timed_section
from api.readiness_model import build_readiness_bundle
from api.readiness_snapshot import resolve_gate_progression
from runtime.pipeline_governance import build_pipeline_governance
from runtime.pipeline_health import (
    build_api_feed_health,
    build_market_rotation_status,
    build_trade_pipeline_health,
)
from runtime.session_identity import build_session_identity_fields
from runtime.session_lock import lock_path_for_scope, read_session_lock, resolve_account_scope
from runtime.app_mode import resolve_app_mode, resolve_data_root
from runtime.unified_execution import cached_unified_routes


def _session_lock_record() -> dict[str, Any] | None:
    try:
        mode = resolve_app_mode()
        scope = resolve_account_scope(mode)
        root = resolve_data_root(mode)
        return read_session_lock(lock_path_for_scope(scope, root))
    except Exception:
        return None


def build_gui_status_fast() -> dict[str, Any]:
    """Cheap telemetry slice for tiered cache — typically < 500ms."""
    with timed_section("gui_status.fast.identity"):
        identity = build_session_identity_fields()
    record = _session_lock_record()
    session_status = str(identity.get("session_status") or "").upper()
    lock_present = record is not None

    with timed_section("gui_status.fast.feeds"):
        feed_health = build_api_feed_health()
    with timed_section("gui_status.fast.pipeline"):
        pipeline_rows = build_trade_pipeline_health()
    with timed_section("gui_status.fast.rotation"):
        rotation_status = build_market_rotation_status()
    with timed_section("gui_status.fast.governance"):
        governance = build_pipeline_governance(
            trade_pipeline_health=pipeline_rows,
            api_feed_health=feed_health,
            market_rotation_status=rotation_status,
        )

    with timed_section("gui_status.fast.routing_cache"):
        routes = cached_unified_routes()

    gate_progression = resolve_gate_progression()
    readiness = build_readiness_bundle(
        gate_progression=gate_progression,
        api_feed_health=feed_health,
        unified_execution_route=routes,
        pipeline_governance=governance.get("pipeline_governance") or {},
        session_governance=governance.get("session_governance") or {},
        session_status=session_status,
    )

    return {
        **identity,
        **readiness,
        "gate_progression": gate_progression,
        "gui_attach_ready": lock_present and session_status == "HEALTHY" and bool(identity.get("session_id")),
        "api_feed_health": feed_health,
        "trade_pipeline_health": pipeline_rows,
        "market_rotation_status": rotation_status,
        "unified_execution_route": routes,
        **governance,
        "snapshot_tier": "fast",
        "snapshot_warming": bool(routes) is False,
    }
