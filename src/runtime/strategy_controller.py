"""
Strategy controller — per-epic execution path ownership and lightweight guards.

Guards only block dispatch when strategy ownership forbids a path; they do not
alter sizing, signals, or LiveExecutor internals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.strategy_profile import StrategyProfile, strategy_profile_from_row
from runtime.strategy_selector import RecommendedStrategyProfile

ROTATION_SCALP_MICRO_THRESHOLD = 70
_DECISIONS_CACHE_TTL_SEC = 1.0

_decisions_override: list[dict[str, Any]] | None = None
_decisions_cache: dict[str, dict[str, Any]] = {}
_decisions_cache_at: float = 0.0


class ExecutionPath(str, Enum):
    PATH_A = "PATH_A"
    MICRO = "MICRO"
    PATH_B_HANDOFF = "PATH_B_HANDOFF"


class StrategyOwnership(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    STAND_DOWN = "STAND_DOWN"
    UNKNOWN = "UNKNOWN"


_ALL_PATHS = [ExecutionPath.PATH_A, ExecutionPath.MICRO, ExecutionPath.PATH_B_HANDOFF]


@dataclass
class StrategyOwnershipDecision:
    epic: str
    ownership: StrategyOwnership
    allowed_paths: list[ExecutionPath]
    blocked_paths: list[ExecutionPath]
    reason: str
    confidence: int
    enforcement_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "ownership": self.ownership.value,
            "allowed_paths": [p.value for p in self.allowed_paths],
            "blocked_paths": [p.value for p in self.blocked_paths],
            "reason": self.reason,
            "confidence": int(self.confidence),
            "enforcement_flags": list(self.enforcement_flags),
        }


@dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reason: str = ""
    ownership: StrategyOwnership = StrategyOwnership.UNKNOWN


def reset_strategy_controller_for_tests() -> None:
    global _decisions_override, _decisions_cache, _decisions_cache_at
    _decisions_override = None
    _decisions_cache = {}
    _decisions_cache_at = 0.0


def set_strategy_controller_decisions_for_tests(decisions: list[dict[str, Any]] | None) -> None:
    global _decisions_override, _decisions_cache, _decisions_cache_at
    _decisions_override = decisions
    _decisions_cache = {row["epic"]: row for row in (decisions or [])}
    _decisions_cache_at = time.time()


def _parse_ownership(raw: str | None) -> StrategyOwnership:
    value = str(raw or StrategyOwnership.UNKNOWN.value).upper()
    try:
        return StrategyOwnership(value)
    except ValueError:
        return StrategyOwnership.UNKNOWN


def _resolve_ownership(
    epic_row: dict[str, Any],
    advice_row: dict[str, Any] | None,
) -> tuple[StrategyOwnership, int, str]:
    raw_active = str(epic_row.get("active_strategy_profile") or "").upper()
    if raw_active == StrategyOwnership.STAND_DOWN.value:
        return StrategyOwnership.STAND_DOWN, 95, f"{StrategyOwnership.STAND_DOWN.value} ownership via active_strategy_profile"

    profile = strategy_profile_from_row(epic_row)
    active = profile.value if profile is not StrategyProfile.UNKNOWN else StrategyOwnership.UNKNOWN.value
    ownership = _parse_ownership(active)
    confidence = 85 if ownership is not StrategyOwnership.UNKNOWN else 40
    source = "active_strategy_profile"

    if ownership is StrategyOwnership.UNKNOWN and advice_row:
        recommended = str(advice_row.get("recommended_strategy_profile") or "").upper()
        ownership = _parse_ownership(recommended)
        if ownership is not StrategyOwnership.UNKNOWN:
            try:
                confidence = int(advice_row.get("confidence") or 50)
            except (TypeError, ValueError):
                confidence = 50
            source = "strategy_selector_advice"

    reason = f"{ownership.value} ownership via {source}"
    return ownership, max(0, min(100, confidence)), reason


def _paths_for_ownership(
    ownership: StrategyOwnership,
    *,
    advice_row: dict[str, Any] | None,
    epic: str = "",
) -> tuple[list[ExecutionPath], list[ExecutionPath], list[str], str]:
    flags: list[str] = []
    reason_suffix = ""

    if ownership is StrategyOwnership.UNKNOWN:
        return list(_ALL_PATHS), [], [], ""

    if ownership is StrategyOwnership.STAND_DOWN:
        flags.append("STAND_DOWN")
        return [], list(_ALL_PATHS), flags, "all execution paths blocked"

    if ownership is StrategyOwnership.SCALP:
        flags.append("SCALP_OWNS_EPIC")
        try:
            from system.dual_regime import sb_macro_path_a_carve_active

            _sb_path_a = sb_macro_path_a_carve_active()
        except Exception:
            _sb_path_a = False
        if _sb_path_a:
            flags.append("SB_MACRO_PATH_A_CARVE")
            allowed = [ExecutionPath.PATH_A]
            blocked = [ExecutionPath.MICRO, ExecutionPath.PATH_B_HANDOFF]
            reason_suffix = (
                "SCALP owns epic — SB macro carve allows Path A; micro blocked"
            )
            return allowed, blocked, flags, reason_suffix
        allowed = [ExecutionPath.MICRO, ExecutionPath.PATH_B_HANDOFF]
        blocked = [ExecutionPath.PATH_A]
        reason_suffix = "SCALP owns epic — micro and Path B handoff only"
        return allowed, blocked, flags, reason_suffix

    if ownership is StrategyOwnership.MOMENTUM:
        flags.append("MOMENTUM_OWNS_EPIC")
        allowed = [ExecutionPath.PATH_A]
        blocked = [ExecutionPath.MICRO, ExecutionPath.PATH_B_HANDOFF]
        reason_suffix = "MOMENTUM owns epic — Path A only"
        return allowed, blocked, flags, reason_suffix

    if ownership is StrategyOwnership.SWING:
        flags.append("SWING_OWNS_EPIC")
        allowed = [ExecutionPath.PATH_A]
        blocked = [ExecutionPath.MICRO, ExecutionPath.PATH_B_HANDOFF]
        reason_suffix = "SWING owns epic — Path A only"
        return allowed, blocked, flags, reason_suffix

    if ownership is StrategyOwnership.ROTATION:
        flags.append("ROTATION_ACTIVE")
        allowed = [ExecutionPath.PATH_B_HANDOFF]
        blocked = [ExecutionPath.PATH_A]
        scalp_advice = False
        if advice_row:
            recommended = str(advice_row.get("recommended_strategy_profile") or "").upper()
            try:
                advice_conf = int(advice_row.get("confidence") or 0)
            except (TypeError, ValueError):
                advice_conf = 0
            scalp_advice = (
                recommended == RecommendedStrategyProfile.SCALP.value
                and advice_conf >= ROTATION_SCALP_MICRO_THRESHOLD
            )
        # Dual-core desk enters via MICRO. With multi_source_auto_rotation always
        # on, ownership stays ROTATION — blocking MICRO forever freezes DOW.
        hot_path_micro = False
        try:
            from runtime.dual_core_execution import epic_allowed_on_hot_path

            hot_path_micro = bool(epic_allowed_on_hot_path(str(epic or "")))
        except Exception:
            hot_path_micro = False
        if scalp_advice or hot_path_micro:
            allowed.append(ExecutionPath.MICRO)
            if scalp_advice:
                flags.append("ROTATION_MICRO_SELECTOR_EXCEPTION")
                reason_suffix = (
                    f"ROTATION active — sweep allowed; micro allowed via SCALP advice "
                    f">= {ROTATION_SCALP_MICRO_THRESHOLD}"
                )
            else:
                flags.append("ROTATION_HOT_PATH_MICRO")
                reason_suffix = (
                    "ROTATION active — hot-path epic may use MICRO (dual_core desk lane)"
                )
        else:
            blocked.append(ExecutionPath.MICRO)
            reason_suffix = "ROTATION active — sweep only; micro blocked without high-confidence SCALP advice"
        return allowed, blocked, flags, reason_suffix

    return list(_ALL_PATHS), [], flags, ""


def set_strategy_controller_decisions_for_test(decisions: list[dict[str, Any]] | None) -> None:
    set_strategy_controller_decisions_for_tests(decisions)


@dataclass
class EpicOwnershipView:
    epic: str
    ownership: StrategyOwnership
    allowed_paths: list[str]
    blocked_paths: list[str]
    reason: str
    confidence: int
    enforcement_flags: list[str] = field(default_factory=list)


def decide_epic_ownership(
    epic_row: dict[str, Any],
    *,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
) -> EpicOwnershipView:
    epic = str(epic_row.get("epic") or "")
    advice_row: dict[str, Any] | None = None
    if strategy_selector_advice:
        for row in strategy_selector_advice:
            if str(row.get("epic") or "") == epic:
                advice_row = row
                break
    decision = decide_epic(epic_row, advice_row=advice_row)
    payload = decision.to_dict()
    return EpicOwnershipView(
        epic=payload["epic"],
        ownership=decision.ownership,
        allowed_paths=list(payload["allowed_paths"]),
        blocked_paths=list(payload["blocked_paths"]),
        reason=payload["reason"],
        confidence=payload["confidence"],
        enforcement_flags=list(payload["enforcement_flags"]),
    )


def is_execution_path_allowed(
    epic: str,
    path: ExecutionPath | str,
) -> tuple[bool, str]:
    result = check_execution_permission(epic, path)
    return result.allowed, result.reason


def decide_epic(
    epic_row: dict[str, Any],
    *,
    advice_row: dict[str, Any] | None = None,
) -> StrategyOwnershipDecision:
    epic = str(epic_row.get("epic") or "")
    ownership, confidence, base_reason = _resolve_ownership(epic_row, advice_row)
    allowed, blocked, flags, path_reason = _paths_for_ownership(
        ownership, advice_row=advice_row, epic=epic
    )
    reason = path_reason or base_reason
    return StrategyOwnershipDecision(
        epic=epic,
        ownership=ownership,
        allowed_paths=allowed,
        blocked_paths=blocked,
        reason=reason,
        confidence=confidence,
        enforcement_flags=flags,
    )


def build_strategy_controller_decisions(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-epic strategy ownership decisions for GUI and guards."""
    if _decisions_override is not None:
        return list(_decisions_override)

    if trade_pipeline_health is None:
        from runtime.pipeline_health import build_trade_pipeline_health

        trade_pipeline_health = build_trade_pipeline_health()

    advice_by_epic: dict[str, dict[str, Any]] = {}
    if strategy_selector_advice is None:
        from runtime.strategy_selector import build_strategy_selector_advice

        strategy_selector_advice = build_strategy_selector_advice(
            trade_pipeline_health=trade_pipeline_health,
            pipeline_governance=pipeline_governance,
            api_feed_health=api_feed_health,
            market_rotation_status=market_rotation_status,
            session_governance=session_governance,
        )
    for row in strategy_selector_advice or []:
        epic = str(row.get("epic") or "")
        if epic:
            advice_by_epic[epic] = row

    decisions: list[dict[str, Any]] = []
    for epic_row in trade_pipeline_health:
        epic = str(epic_row.get("epic") or "")
        if not epic:
            continue
        decision = decide_epic(epic_row, advice_row=advice_by_epic.get(epic))
        decisions.append(decision.to_dict())

    global _decisions_cache, _decisions_cache_at
    _decisions_cache = {row["epic"]: row for row in decisions}
    _decisions_cache_at = time.time()
    return decisions


def _decision_for_epic(epic: str) -> dict[str, Any] | None:
    if _decisions_override is not None:
        return _decisions_cache.get(epic)

    now = time.time()
    if not _decisions_cache or (now - _decisions_cache_at) > _DECISIONS_CACHE_TTL_SEC:
        build_strategy_controller_decisions()
    return _decisions_cache.get(epic)


def is_path_allowed(
    epic: str,
    path: ExecutionPath | str,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if isinstance(path, str):
        path = ExecutionPath(path)
    if decisions is not None:
        row = decisions.get(epic)
    else:
        row = _decision_for_epic(epic)
    if not row:
        return True
    allowed = set(row.get("allowed_paths") or [])
    if not allowed and row.get("ownership") == StrategyOwnership.UNKNOWN.value:
        return True
    return path.value in allowed


def check_execution_permission(
    epic: str,
    path: ExecutionPath | str,
) -> PermissionResult:
    try:
        import os

        from system.demo_execution_plane import execution_guards_relaxed

        if os.environ.get("IG_AGENT_PYTEST") != "1" and execution_guards_relaxed(
            epic=epic
        ):
            return PermissionResult(allowed=True)
    except Exception:
        pass
    if isinstance(path, str):
        path = ExecutionPath(path)
    row = _decision_for_epic(epic)
    if not row:
        return PermissionResult(allowed=True)
    ownership = _parse_ownership(row.get("ownership"))
    if is_path_allowed(epic, path, decisions={epic: row}):
        return PermissionResult(allowed=True, ownership=ownership)
    blocked = row.get("blocked_paths") or []
    if path.value in blocked:
        reason = str(row.get("reason") or f"{path.value} blocked by strategy ownership")
        return PermissionResult(allowed=False, reason=reason, ownership=ownership)
    return PermissionResult(allowed=False, reason="blocked_by_strategy_controller", ownership=ownership)


def _log_blocked(epic: str, path: ExecutionPath, reason: str) -> None:
    try:
        from system.logging_engine import log_engine

        log_engine(
            f"StrategyController: blocked_by_strategy_controller epic={epic} "
            f"path={path.value} reason={reason}"
        )
    except Exception:
        pass


def guard_path_a_execution(epic: str) -> bool:
    result = check_execution_permission(epic, ExecutionPath.PATH_A)
    if not result.allowed:
        _log_blocked(epic, ExecutionPath.PATH_A, result.reason or "blocked_by_strategy_controller")
        return False
    return True


def guard_micro_dispatch(epic: str) -> bool:
    result = check_execution_permission(epic, ExecutionPath.MICRO)
    if not result.allowed:
        _log_blocked(epic, ExecutionPath.MICRO, result.reason or "blocked_by_strategy_controller")
        return False
    return True


def guard_path_b_handoff(epic: str) -> bool:
    result = check_execution_permission(epic, ExecutionPath.PATH_B_HANDOFF)
    if not result.allowed:
        _log_blocked(epic, ExecutionPath.PATH_B_HANDOFF, result.reason or "blocked_by_strategy_controller")
        return False
    return True
