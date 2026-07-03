"""
Unified execution engine — Phase 10 strategy-driven routing layer (v40).

Merges micro, Path A, and Path B into unified advisory routing decisions.
Does NOT modify signals, LiveExecutor internals, REST, or risk manager logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.regime_detection import MarketRegime
from runtime.strategy_controller import ExecutionPath
from runtime.strategy_selector import _feed_degraded, _governance_for_epic

_OVERRIDE: list[dict[str, Any]] | None = None
_ROUTE_CACHE: dict[str, dict[str, Any]] = {}
_ROUTE_CACHE_AT: float = 0.0
_ROUTE_CACHE_TTL_SEC = 3.0
_ROUTE_REFRESH_STOP = __import__("threading").Event()
_ROUTE_REFRESH_THREAD: __import__("threading").Thread | None = None


class UnifiedExecutionPath(str, Enum):
    MICRO = "MICRO"
    PATH_A = "PATH_A"
    PATH_B_SWEEP = "PATH_B_SWEEP"
    NONE = "NONE"


_PROFILE_PRIMARY: dict[str, UnifiedExecutionPath] = {
    "SCALP": UnifiedExecutionPath.MICRO,
    "MOMENTUM": UnifiedExecutionPath.PATH_A,
    "SWING": UnifiedExecutionPath.PATH_A,
    "ROTATION": UnifiedExecutionPath.PATH_B_SWEEP,
    "STAND_DOWN": UnifiedExecutionPath.NONE,
}

_PATH_TO_EXECUTION: dict[str, str] = {
    UnifiedExecutionPath.MICRO.value: ExecutionPath.MICRO.value,
    UnifiedExecutionPath.PATH_A.value: ExecutionPath.PATH_A.value,
    UnifiedExecutionPath.PATH_B_SWEEP.value: ExecutionPath.PATH_B_HANDOFF.value,
}


@dataclass
class UnifiedExecutionRoute:
    epic: str
    execution_path: str
    route_confidence: int
    route_reason: str
    route_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "execution_path": self.execution_path,
            "route_confidence": int(self.route_confidence),
            "route_reason": self.route_reason,
            "route_flags": sorted(set(self.route_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_unified_execution_for_tests() -> None:
    global _OVERRIDE, _ROUTE_CACHE, _ROUTE_CACHE_AT
    _OVERRIDE = None
    _ROUTE_CACHE = {}
    _ROUTE_CACHE_AT = 0.0


def invalidate_unified_route_cache() -> None:
    """Clear route cache after advisory chain or config change."""
    global _ROUTE_CACHE, _ROUTE_CACHE_AT
    _ROUTE_CACHE = {}
    _ROUTE_CACHE_AT = 0.0


def set_unified_execution_routes_for_tests(routes: list[dict[str, Any]] | None) -> None:
    global _OVERRIDE, _ROUTE_CACHE, _ROUTE_CACHE_AT
    import time

    _OVERRIDE = routes
    _ROUTE_CACHE = {row["epic"]: row for row in (routes or [])}
    _ROUTE_CACHE_AT = time.time()


def _index_by_epic(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(r["epic"]): r for r in (rows or []) if r.get("epic")}


def _path_blocked(
    path: UnifiedExecutionPath,
    *,
    hard_row: dict[str, Any] | None,
    soft_row: dict[str, Any] | None,
    hard_active: bool,
) -> bool:
    if path is UnifiedExecutionPath.NONE:
        return False
    exec_path = _PATH_TO_EXECUTION.get(path.value, path.value)
    if hard_row and hard_row.get("active"):
        blocked = set(hard_row.get("hard_block_paths") or [])
        if exec_path in blocked:
            return True
        allowed = set(hard_row.get("hard_allow_paths") or [])
        if allowed and exec_path not in allowed:
            return True
    if not hard_active and soft_row:
        blocked = set(soft_row.get("soft_block_paths") or [])
        if exec_path in blocked:
            return True
    return False


def _fallback_path(profile: str, primary: UnifiedExecutionPath) -> UnifiedExecutionPath:
    profile = profile.upper()
    if profile == "SCALP" and primary is UnifiedExecutionPath.MICRO:
        return UnifiedExecutionPath.PATH_A
    if profile == "MOMENTUM" and primary is UnifiedExecutionPath.PATH_A:
        return UnifiedExecutionPath.MICRO
    if profile == "SWING" and primary is UnifiedExecutionPath.PATH_A:
        return UnifiedExecutionPath.NONE
    if profile == "ROTATION" and primary is UnifiedExecutionPath.PATH_B_SWEEP:
        return UnifiedExecutionPath.MICRO
    return UnifiedExecutionPath.NONE


def _resolve_strategy(
    selector_row: dict[str, Any] | None,
    hard_row: dict[str, Any] | None,
) -> str:
    if hard_row and hard_row.get("active"):
        flags = set(hard_row.get("enforcement_flags") or [])
        if "STAND_DOWN_HARD" in flags or not hard_row.get("hard_allow_paths"):
            return "STAND_DOWN"
    return str((selector_row or {}).get("recommended_profile") or "MOMENTUM").upper()


def _regime_path_boost(regime: str) -> dict[str, int]:
    boosts = {p.value: 0 for p in UnifiedExecutionPath}
    regime = regime.upper()
    if regime == MarketRegime.TREND.value:
        boosts[UnifiedExecutionPath.PATH_A.value] += 15
    elif regime == MarketRegime.CHOP.value:
        boosts[UnifiedExecutionPath.MICRO.value] += 15
    elif regime == MarketRegime.BREAKOUT.value:
        boosts[UnifiedExecutionPath.PATH_A.value] += 12
    elif regime == MarketRegime.REVERSAL.value:
        boosts[UnifiedExecutionPath.PATH_B_SWEEP.value] += 15
    elif regime == MarketRegime.EXTREME_VOL.value:
        boosts[UnifiedExecutionPath.NONE.value] += 20
    elif regime == MarketRegime.LOW_VOL.value:
        boosts[UnifiedExecutionPath.PATH_A.value] += 10
    elif regime == MarketRegime.LIQUIDITY_DROP.value:
        boosts[UnifiedExecutionPath.NONE.value] += 25
    return boosts


def _risk_path_boost(risk_profile: str, sizing_factor: float) -> dict[str, int]:
    boosts = {p.value: 0 for p in UnifiedExecutionPath}
    risk = risk_profile.upper()
    if risk == "TIGHT":
        boosts[UnifiedExecutionPath.MICRO.value] += 10
        boosts[UnifiedExecutionPath.PATH_A.value] -= 5
    elif risk == "WIDE":
        boosts[UnifiedExecutionPath.PATH_A.value] += 12
    elif risk == "STRUCTURAL":
        boosts[UnifiedExecutionPath.PATH_B_SWEEP.value] += 10
    elif risk == "ZERO":
        boosts[UnifiedExecutionPath.NONE.value] += 30
    if sizing_factor < 0.15:
        boosts[UnifiedExecutionPath.MICRO.value] += 5
    elif sizing_factor > 0.35:
        boosts[UnifiedExecutionPath.PATH_A.value] += 5
    return boosts


def decide_epic_unified_route(
    epic: str,
    *,
    selector_row: dict[str, Any] | None = None,
    regime_row: dict[str, Any] | None = None,
    risk_row: dict[str, Any] | None = None,
    sizing_row: dict[str, Any] | None = None,
    daily_targeting: dict[str, Any] | None = None,
    hard_row: dict[str, Any] | None = None,
    soft_row: dict[str, Any] | None = None,
    controller_row: dict[str, Any] | None = None,
    transition_row: dict[str, Any] | None = None,
    gov_row: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive unified execution route for one epic."""
    flags: list[str] = []
    reasons: list[str] = []

    strategy = _resolve_strategy(selector_row, hard_row)
    regime = str((regime_row or {}).get("regime_classification") or MarketRegime.UNKNOWN.value).upper()
    try:
        regime_conf = int((regime_row or {}).get("regime_confidence") or 0)
    except (TypeError, ValueError):
        regime_conf = 0
    try:
        selector_conf = int((selector_row or {}).get("selector_confidence") or 50)
    except (TypeError, ValueError):
        selector_conf = 50
    try:
        risk_conf = int((risk_row or {}).get("risk_confidence") or 50)
    except (TypeError, ValueError):
        risk_conf = 50
    try:
        sizing_conf = int((sizing_row or {}).get("sizing_confidence") or 50)
    except (TypeError, ValueError):
        sizing_conf = 50
    try:
        sizing_factor = float((sizing_row or {}).get("recommended_size_factor") or 0.25)
    except (TypeError, ValueError):
        sizing_factor = 0.25

    risk_profile = str((risk_row or {}).get("risk_profile") or "MEDIUM").upper()
    hard_active = bool(hard_row and hard_row.get("active"))

    profile_flags = {
        "SCALP": "SCALP_ROUTE",
        "MOMENTUM": "MOMENTUM_ROUTE",
        "SWING": "SWING_ROUTE",
        "ROTATION": "ROTATION_ROUTE",
        "STAND_DOWN": "STAND_DOWN_ROUTE",
    }

    if strategy == "STAND_DOWN" or regime == MarketRegime.LIQUIDITY_DROP.value:
        route_path = UnifiedExecutionPath.NONE
        flags.append(profile_flags.get(strategy, "STAND_DOWN_ROUTE"))
        if regime == MarketRegime.LIQUIDITY_DROP.value:
            flags.append("REGIME_NONE_ROUTE")
        reasons.append("STAND_DOWN or liquidity drop — no execution route")
    else:
        primary = _PROFILE_PRIMARY.get(strategy, UnifiedExecutionPath.PATH_A)
        flags.append(profile_flags.get(strategy, "MOMENTUM_ROUTE"))
        reasons.append(f"{strategy} primary route → {primary.value}")

        if _path_blocked(primary, hard_row=hard_row, soft_row=soft_row, hard_active=hard_active):
            fallback = _fallback_path(strategy, primary)
            if fallback is not UnifiedExecutionPath.NONE and not _path_blocked(
                fallback, hard_row=hard_row, soft_row=soft_row, hard_active=hard_active
            ):
                route_path = fallback
                flags.append("UNIFIED_FALLBACK_ROUTE")
                reasons.append(f"primary {primary.value} blocked — fallback {fallback.value}")
            else:
                route_path = UnifiedExecutionPath.NONE
                flags.append("ALL_PATHS_BLOCKED")
                reasons.append("primary and fallback paths blocked")
        else:
            route_path = primary

    # Regime overrides for NONE/high-risk regimes
    regime_boosts = _regime_path_boost(regime)
    risk_boosts = _risk_path_boost(risk_profile, sizing_factor)

    if regime_boosts.get(UnifiedExecutionPath.NONE.value, 0) >= 20 and route_path is not UnifiedExecutionPath.NONE:
        if regime in (MarketRegime.EXTREME_VOL.value, MarketRegime.LIQUIDITY_DROP.value):
            route_path = UnifiedExecutionPath.NONE
            flags.append("REGIME_NONE_ROUTE")
            reasons.append(f"{regime} regime — route suppressed")

    # Daily targeting — suppress only when explicitly ahead-of-target (not enforcement dampening)
    daily_flags = list((daily_targeting or {}).get("bias_flags") or [])
    progress_band = str(
        ((daily_targeting or {}).get("contributing_factors") or {})
        .get("session_progress", {})
        .get("band", "")
    ).lower()
    ahead_of_target = (
        "AHEAD_OF_TARGET_PROTECTION" in daily_flags or progress_band == "ahead"
    )
    if ahead_of_target and route_path is not UnifiedExecutionPath.NONE:
        route_path = UnifiedExecutionPath.NONE
        flags.append("DAILY_TARGET_PROTECTION")
        reasons.append("daily P&L ahead-of-target — route suppressed")

    if _feed_degraded(api_feed_health or {}):
        if route_path is UnifiedExecutionPath.PATH_B_SWEEP:
            route_path = UnifiedExecutionPath.MICRO
            flags.append("FEED_DEGRADED_PATH_DEGRADE")
            reasons.append("degraded feed — Path B degraded to MICRO probe")

    # Confidence
    path_boost = regime_boosts.get(route_path.value, 0) + risk_boosts.get(route_path.value, 0)
    confidence = max(
        20,
        min(
            98,
            int(
                selector_conf * 0.30
                + regime_conf * 0.25
                + risk_conf * 0.20
                + sizing_conf * 0.15
                + path_boost
            ),
        ),
    )

    if regime_boosts.get(route_path.value, 0) > 0:
        flags.append("REGIME_ROUTE_BOOST")
    if risk_boosts.get(route_path.value, 0) > 0:
        flags.append("RISK_ROUTE_BOOST")

    gov_anomalies = list(gov_row.get("pipeline_anomalies") or []) if gov_row else []
    if gov_anomalies:
        confidence = max(20, confidence - min(20, len(gov_anomalies) * 5))
        flags.append("GOVERNANCE_ROUTE_PENALTY")

    factors = {
        "strategy": strategy,
        "regime": regime,
        "risk": risk_profile,
        "sizing_factor": round(sizing_factor, 4),
        "daily_target_band": ((daily_targeting or {}).get("contributing_factors") or {})
        .get("session_progress", {})
        .get("band"),
        "enforcement": {
            "hard_active": hard_active,
            "hard_allow": list((hard_row or {}).get("hard_allow_paths") or []),
            "hard_block": list((hard_row or {}).get("hard_block_paths") or []),
        },
        "governance": {"anomalies": gov_anomalies},
        "feed_health": "degraded" if _feed_degraded(api_feed_health or {}) else "ok",
        "controller_ownership": (controller_row or {}).get("ownership"),
        "transition_target": (transition_row or {}).get("target_profile"),
    }

    route = UnifiedExecutionRoute(
        epic=epic,
        execution_path=route_path.value,
        route_confidence=confidence,
        route_reason="; ".join(reasons[:4]),
        route_flags=flags,
        contributing_factors=factors,
    )
    return route.to_dict()


def build_unified_execution_routes(
    *,
    regime_detection: list[dict[str, Any]] | None = None,
    regime_aware_strategy_selector: list[dict[str, Any]] | None = None,
    regime_risk_envelope: list[dict[str, Any]] | None = None,
    regime_sizing_advice: list[dict[str, Any]] | None = None,
    daily_pnl_targeting: dict[str, Any] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    soft_enforcement_decisions: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build unified execution routes for all monitored epics."""
    if _OVERRIDE is not None:
        return list(_OVERRIDE)

    regime_by = _index_by_epic(regime_detection)
    selector_by = _index_by_epic(regime_aware_strategy_selector)
    risk_by = _index_by_epic(regime_risk_envelope)
    sizing_by = _index_by_epic(regime_sizing_advice)
    hard_by = _index_by_epic(hard_enforcement_decisions)
    soft_by = _index_by_epic(soft_enforcement_decisions)
    controller_by = _index_by_epic(strategy_controller_decisions)
    transition_by = _index_by_epic(strategy_transition_advice)

    epics: list[str] = []
    for row in trade_pipeline_health or []:
        epic = str(row.get("epic") or "")
        if epic and epic not in epics:
            epics.append(epic)
    for epic in selector_by:
        if epic not in epics:
            epics.append(epic)

    routes = [
        decide_epic_unified_route(
            epic,
            selector_row=selector_by.get(epic),
            regime_row=regime_by.get(epic),
            risk_row=risk_by.get(epic),
            sizing_row=sizing_by.get(epic),
            daily_targeting=daily_pnl_targeting,
            hard_row=hard_by.get(epic),
            soft_row=soft_by.get(epic),
            controller_row=controller_by.get(epic),
            transition_row=transition_by.get(epic),
            gov_row=_governance_for_epic(epic, pipeline_governance or {}),
            api_feed_health=api_feed_health,
        )
        for epic in epics
    ]

    global _ROUTE_CACHE, _ROUTE_CACHE_AT
    import time

    _ROUTE_CACHE = {row["epic"]: row for row in routes}
    _ROUTE_CACHE_AT = time.time()
    return routes


def _route_for_epic(epic: str) -> dict[str, Any] | None:
    return _ROUTE_CACHE.get(str(epic or ""))


def cached_unified_routes() -> list[dict[str, Any]]:
    """Read-only route cache for telemetry (/api/state) — no advisory rebuild."""
    import time

    if _ROUTE_CACHE and (time.time() - _ROUTE_CACHE_AT) > _ROUTE_CACHE_TTL_SEC:
        _trigger_route_cache_refresh_async()
    return list(_ROUTE_CACHE.values())


def apply_route_cache_rows(routes: list[dict[str, Any]] | None) -> None:
    """Update route cache from pre-built advisory rows (background/gui snapshot)."""
    global _ROUTE_CACHE, _ROUTE_CACHE_AT
    import time

    rows = [r for r in (routes or []) if isinstance(r, dict) and r.get("epic")]
    if not rows:
        return
    _ROUTE_CACHE = {str(row["epic"]): row for row in rows}
    _ROUTE_CACHE_AT = time.time()


def _trigger_route_cache_refresh_async() -> None:
    def _run() -> None:
        try:
            from api.readiness_snapshot import get_gui_snapshot
            from api.endpoint_profiler import timed_section

            with timed_section("routing.cache_refresh_from_gui"):
                snap = get_gui_snapshot()
                routes = snap.get("unified_execution_route")
                if isinstance(routes, list) and routes:
                    apply_route_cache_rows(routes)
        except Exception:
            pass

    __import__("threading").Thread(
        target=_run,
        name="unified-route-cache-refresh",
        daemon=True,
    ).start()


def start_unified_route_cache_refresher(interval_sec: float = 3.0) -> None:
    """Daemon: keep execution route cache aligned with latest GUI advisory snapshot."""
    global _ROUTE_REFRESH_THREAD
    if _ROUTE_REFRESH_THREAD is not None and _ROUTE_REFRESH_THREAD.is_alive():
        return
    _ROUTE_REFRESH_STOP.clear()

    def _loop() -> None:
        while not _ROUTE_REFRESH_STOP.is_set():
            try:
                from api.readiness_snapshot import get_gui_snapshot

                snap = get_gui_snapshot()
                routes = snap.get("unified_execution_route")
                if isinstance(routes, list) and routes:
                    apply_route_cache_rows(routes)
            except Exception:
                pass
            if _ROUTE_REFRESH_STOP.wait(interval_sec):
                break

    _ROUTE_REFRESH_THREAD = __import__("threading").Thread(
        target=_loop,
        name="unified-route-refresher",
        daemon=True,
    )
    _ROUTE_REFRESH_THREAD.start()


def stop_unified_route_cache_refresher() -> None:
    _ROUTE_REFRESH_STOP.set()


def _path_allowed_by_route(epic: str, attempted: UnifiedExecutionPath) -> tuple[bool, str]:
    import os

    try:
        from system.demo_execution_plane import execution_guards_relaxed

        if os.environ.get("IG_AGENT_PYTEST") != "1" and execution_guards_relaxed(epic=epic):
            return True, ""
    except Exception:
        pass
    row = _route_for_epic(epic)
    if not row:
        return True, ""
    route_path = str(row.get("execution_path") or UnifiedExecutionPath.NONE.value)
    if route_path == UnifiedExecutionPath.NONE.value:
        return False, str(row.get("route_reason") or "unified route NONE")
    if route_path != attempted.value:
        return False, (
            f"unified route={route_path} does not allow {attempted.value}: "
            f"{row.get('route_reason', '')}"
        )
    return True, ""


def _log_unified_blocked(epic: str, path: UnifiedExecutionPath, reason: str) -> None:
    try:
        from system.logging_engine import log_engine

        log_engine(
            f"UnifiedExecution: blocked_by_unified_execution_route epic={epic} "
            f"path={path.value} reason={reason}"
        )
    except Exception:
        pass


def unified_guard_path_a_execution(epic: str) -> bool:
    allowed, reason = _path_allowed_by_route(epic, UnifiedExecutionPath.PATH_A)
    if not allowed:
        _log_unified_blocked(epic, UnifiedExecutionPath.PATH_A, reason)
        return False
    return True


def unified_guard_micro_dispatch(epic: str) -> bool:
    allowed, reason = _path_allowed_by_route(epic, UnifiedExecutionPath.MICRO)
    if not allowed:
        _log_unified_blocked(epic, UnifiedExecutionPath.MICRO, reason)
        return False
    return True


def unified_guard_path_b_handoff(epic: str) -> bool:
    allowed, reason = _path_allowed_by_route(epic, UnifiedExecutionPath.PATH_B_SWEEP)
    if not allowed:
        _log_unified_blocked(epic, UnifiedExecutionPath.PATH_B_SWEEP, reason)
        return False
    return True
