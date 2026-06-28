"""
Hard enforcement engine — Phase 2 execution binding.

Deterministic path gating that overrides soft enforcement when active.
Does NOT alter signals, sizing, LiveExecutor, or Path A/B/micro plumbing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.strategy_controller import ExecutionPath
from runtime.strategy_selector import CRITICAL_ANOMALIES, _feed_degraded, _governance_for_epic

TRANSITION_CONFIDENCE_THRESHOLD = 85
ROTATION_SCALP_SELECTOR_THRESHOLD = 85

_DECISIONS_OVERRIDE: list[dict[str, Any]] | None = None
_DECISIONS_CACHE: dict[str, dict[str, Any]] = {}
_DECISIONS_CACHE_AT: float = 0.0
_DECISIONS_CACHE_TTL_SEC = 1.0

_ALL_PATH_VALUES = [p.value for p in ExecutionPath]

_HARD_PROFILE_PATHS: dict[str, list[str]] = {
    "SCALP": [ExecutionPath.MICRO.value],
    "MOMENTUM": [ExecutionPath.PATH_A.value],
    "SWING": [ExecutionPath.PATH_A.value],
    "ROTATION": [ExecutionPath.PATH_B_HANDOFF.value],
    "STAND_DOWN": [],
}


class HardOwnership(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    STAND_DOWN = "STAND_DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass
class HardEnforcementDecision:
    epic: str
    hard_block_paths: list[str]
    hard_allow_paths: list[str]
    enforcement_confidence: int
    enforcement_reason: str
    enforcement_flags: list[str] = field(default_factory=list)
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "hard_block_paths": sorted(set(self.hard_block_paths)),
            "hard_allow_paths": sorted(set(self.hard_allow_paths)),
            "enforcement_confidence": int(self.enforcement_confidence),
            "enforcement_reason": self.enforcement_reason,
            "enforcement_flags": sorted(set(self.enforcement_flags)),
            "active": bool(self.active),
        }


def reset_hard_enforcement_for_tests() -> None:
    global _DECISIONS_OVERRIDE, _DECISIONS_CACHE, _DECISIONS_CACHE_AT
    _DECISIONS_OVERRIDE = None
    _DECISIONS_CACHE = {}
    _DECISIONS_CACHE_AT = 0.0


def set_hard_enforcement_decisions_for_tests(decisions: list[dict[str, Any]] | None) -> None:
    global _DECISIONS_OVERRIDE, _DECISIONS_CACHE, _DECISIONS_CACHE_AT
    _DECISIONS_OVERRIDE = decisions
    _DECISIONS_CACHE = {row["epic"]: row for row in (decisions or [])}
    _DECISIONS_CACHE_AT = time.time()


def _parse_ownership(raw: str | None) -> HardOwnership:
    value = str(raw or HardOwnership.UNKNOWN.value).upper()
    try:
        return HardOwnership(value)
    except ValueError:
        return HardOwnership.UNKNOWN


def _selector_scalp_high(selector_advice: dict[str, Any] | None, threshold: int) -> bool:
    if not selector_advice:
        return False
    rec = str(selector_advice.get("recommended_strategy_profile") or "").upper()
    try:
        conf = int(selector_advice.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0
    return rec == "SCALP" and conf >= threshold


def _governance_critical(gov_row: dict[str, Any]) -> bool:
    anomalies = set(gov_row.get("pipeline_anomalies") or [])
    anomalies.update(gov_row.get("feed_anomalies") or [])
    return bool(anomalies & CRITICAL_ANOMALIES)


def _activation_triggers(
    *,
    controller_row: dict[str, Any] | None,
    transition_row: dict[str, Any] | None,
    ownership: HardOwnership,
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
) -> tuple[bool, list[str]]:
    flags: list[str] = []
    blocked = list((controller_row or {}).get("blocked_paths") or [])
    try:
        transition_conf = int((transition_row or {}).get("transition_confidence") or 0)
    except (TypeError, ValueError):
        transition_conf = 0

    active = False
    if blocked:
        active = True
        flags.append("CONTROLLER_BLOCKED_PATHS")
    if transition_conf >= TRANSITION_CONFIDENCE_THRESHOLD:
        active = True
    if ownership is HardOwnership.STAND_DOWN:
        active = True
    if _governance_critical(gov_row):
        active = True
        flags.append("PIPELINE_CRITICAL")
    if _feed_degraded(api_feed_health):
        active = True
        flags.append("FEED_DEGRADED")
    return active, flags


def decide_epic_hard_enforcement(
    epic: str,
    *,
    controller_row: dict[str, Any] | None,
    transition_row: dict[str, Any] | None,
    selector_advice: dict[str, Any] | None,
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
) -> HardEnforcementDecision:
    """Derive hard enforcement for one epic — execution binding when active."""
    ownership = _parse_ownership(
        (controller_row or {}).get("ownership")
        or (controller_row or {}).get("active_strategy_profile")
    )
    try:
        controller_conf = int((controller_row or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        controller_conf = 0
    try:
        transition_conf = int((transition_row or {}).get("transition_confidence") or 0)
    except (TypeError, ValueError):
        transition_conf = 0
    try:
        selector_conf = int((selector_advice or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        selector_conf = 0

    enforcement_confidence = max(controller_conf, transition_conf, selector_conf)
    hard_block: set[str] = set()
    hard_allow: set[str] = set()
    flags: list[str] = []
    reasons: list[str] = []

    active, trigger_flags = _activation_triggers(
        controller_row=controller_row,
        transition_row=transition_row,
        ownership=ownership,
        gov_row=gov_row,
        api_feed_health=api_feed_health,
    )
    flags.extend(trigger_flags)

    if not active:
        return HardEnforcementDecision(
            epic=epic,
            hard_block_paths=[],
            hard_allow_paths=[],
            enforcement_confidence=min(100, enforcement_confidence),
            enforcement_reason="hard enforcement idle — no activation triggers",
            enforcement_flags=flags,
            active=False,
        )

    if ownership is HardOwnership.STAND_DOWN:
        hard_block.update(_ALL_PATH_VALUES)
        hard_allow.clear()
        flags.append("STAND_DOWN_HARD")
        reasons.append("STAND_DOWN — all execution paths hard-blocked")

    elif ownership is HardOwnership.SCALP:
        hard_allow.add(ExecutionPath.MICRO.value)
        hard_block.update([ExecutionPath.PATH_A.value, ExecutionPath.PATH_B_HANDOFF.value])
        flags.append("SCALP_HARD_ENFORCEMENT")
        reasons.append("SCALP ownership — micro only; Path A and Path B hard-blocked")

    elif ownership is HardOwnership.MOMENTUM:
        hard_allow.add(ExecutionPath.PATH_A.value)
        hard_block.update([ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value])
        flags.append("MOMENTUM_HARD_ENFORCEMENT")
        reasons.append("MOMENTUM ownership — Path A only")

    elif ownership is HardOwnership.SWING:
        hard_allow.add(ExecutionPath.PATH_A.value)
        hard_block.update([ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value])
        flags.append("SWING_HARD_ENFORCEMENT")
        reasons.append("SWING ownership — Path A only")

    elif ownership is HardOwnership.ROTATION:
        hard_allow.add(ExecutionPath.PATH_B_HANDOFF.value)
        hard_block.update([ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value])
        flags.append("ROTATION_HARD_ENFORCEMENT")
        if _selector_scalp_high(selector_advice, ROTATION_SCALP_SELECTOR_THRESHOLD):
            hard_block.discard(ExecutionPath.MICRO.value)
            hard_allow.add(ExecutionPath.MICRO.value)
            flags.append("ROTATION_SCALP_EXCEPTION")
            reasons.append("ROTATION — micro permitted via high-confidence SCALP selector")
        else:
            reasons.append("ROTATION — sweep allowed; execution hard-blocked")

    elif ownership is HardOwnership.UNKNOWN:
        hard_block.update(_ALL_PATH_VALUES)
        hard_allow.clear()
        reasons.append("hard enforcement active — unknown ownership")

    if transition_row and transition_conf >= TRANSITION_CONFIDENCE_THRESHOLD:
        current = str(transition_row.get("current_profile") or "").upper()
        target = str(transition_row.get("target_profile") or "").upper()
        if current and target and current != target:
            flags.append("HIGH_CONFIDENCE_HARD_TRANSITION")
            for path in _HARD_PROFILE_PATHS.get(current, []):
                hard_block.add(path)
            for path in _HARD_PROFILE_PATHS.get(target, []):
                hard_allow.add(path)
                hard_block.discard(path)
            reasons.append(f"high-confidence hard transition {current} → {target}")

    if not reasons:
        reasons.append("hard enforcement active")

    return HardEnforcementDecision(
        epic=epic,
        hard_block_paths=sorted(hard_block),
        hard_allow_paths=sorted(hard_allow),
        enforcement_confidence=min(100, enforcement_confidence),
        enforcement_reason="; ".join(reasons),
        enforcement_flags=flags,
        active=True,
    )


def build_hard_enforcement_decisions(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-epic hard enforcement decisions for GUI and guards."""
    if _DECISIONS_OVERRIDE is not None:
        return list(_DECISIONS_OVERRIDE)

    if trade_pipeline_health is None:
        from runtime.pipeline_governance import build_pipeline_governance
        from runtime.pipeline_health import build_api_feed_health, build_trade_pipeline_health
        from runtime.strategy_controller import build_strategy_controller_decisions
        from runtime.strategy_selector import build_strategy_selector_advice
        from runtime.strategy_transition import build_strategy_transition_advice

        trade_pipeline_health = build_trade_pipeline_health()
        if api_feed_health is None:
            api_feed_health = build_api_feed_health()
        if pipeline_governance is None:
            bundle = build_pipeline_governance(
                trade_pipeline_health=trade_pipeline_health,
                api_feed_health=api_feed_health,
            )
            pipeline_governance = bundle.get("pipeline_governance") or {}
        if strategy_selector_advice is None:
            strategy_selector_advice = build_strategy_selector_advice(
                trade_pipeline_health=trade_pipeline_health,
                pipeline_governance=pipeline_governance,
                api_feed_health=api_feed_health,
            )
        if strategy_controller_decisions is None:
            strategy_controller_decisions = build_strategy_controller_decisions(
                trade_pipeline_health=trade_pipeline_health,
                pipeline_governance=pipeline_governance,
                strategy_selector_advice=strategy_selector_advice,
            )
        if strategy_transition_advice is None:
            strategy_transition_advice = build_strategy_transition_advice(
                trade_pipeline_health=trade_pipeline_health,
                pipeline_governance=pipeline_governance,
                api_feed_health=api_feed_health,
                strategy_selector_advice=strategy_selector_advice,
            )

    if api_feed_health is None:
        from runtime.pipeline_health import build_api_feed_health

        api_feed_health = build_api_feed_health()
    if pipeline_governance is None:
        pipeline_governance = {}

    controller_by_epic = {r["epic"]: r for r in (strategy_controller_decisions or []) if r.get("epic")}
    transition_by_epic = {r["epic"]: r for r in (strategy_transition_advice or []) if r.get("epic")}
    selector_by_epic = {r["epic"]: r for r in (strategy_selector_advice or []) if r.get("epic")}

    decisions: list[dict[str, Any]] = []
    for epic_row in trade_pipeline_health or []:
        epic = str(epic_row.get("epic") or "")
        if not epic:
            continue
        decision = decide_epic_hard_enforcement(
            epic,
            controller_row=controller_by_epic.get(epic),
            transition_row=transition_by_epic.get(epic),
            selector_advice=selector_by_epic.get(epic),
            gov_row=_governance_for_epic(epic, pipeline_governance),
            api_feed_health=api_feed_health,
        )
        decisions.append(decision.to_dict())

    global _DECISIONS_CACHE, _DECISIONS_CACHE_AT
    _DECISIONS_CACHE = {row["epic"]: row for row in decisions}
    _DECISIONS_CACHE_AT = time.time()
    return decisions


def _decision_for_epic(epic: str) -> dict[str, Any] | None:
    if _DECISIONS_OVERRIDE is not None:
        return _DECISIONS_CACHE.get(epic)
    now = time.time()
    if not _DECISIONS_CACHE or (now - _DECISIONS_CACHE_AT) > _DECISIONS_CACHE_TTL_SEC:
        build_hard_enforcement_decisions()
    return _DECISIONS_CACHE.get(epic)


def is_hard_enforcement_active(epic: str) -> bool:
    row = _decision_for_epic(str(epic or ""))
    if not row:
        return False
    return bool(row.get("active"))


def is_path_hard_allowed(epic: str, path: ExecutionPath | str) -> tuple[bool, str]:
    path_value = path.value if isinstance(path, ExecutionPath) else str(path)
    row = _decision_for_epic(str(epic or ""))
    if not row or not row.get("active"):
        return True, ""
    blocked = set(row.get("hard_block_paths") or [])
    if path_value in blocked:
        return False, str(row.get("enforcement_reason") or "hard_blocked_by_strategy_enforcement")
    allowed = set(row.get("hard_allow_paths") or [])
    if allowed and path_value not in allowed:
        return False, str(row.get("enforcement_reason") or "hard_blocked_by_strategy_enforcement")
    return True, ""


def _log_hard_blocked(epic: str, path: ExecutionPath, reason: str) -> None:
    try:
        from system.logging_engine import log_engine

        log_engine(
            f"HardEnforcement: hard_blocked_by_strategy_enforcement epic={epic} "
            f"path={path.value} reason={reason}"
        )
    except Exception:
        pass


def hard_guard_path_a_execution(epic: str) -> bool:
    allowed, reason = is_path_hard_allowed(epic, ExecutionPath.PATH_A)
    if not allowed:
        _log_hard_blocked(epic, ExecutionPath.PATH_A, reason)
        return False
    return True


def hard_guard_micro_dispatch(epic: str) -> bool:
    allowed, reason = is_path_hard_allowed(epic, ExecutionPath.MICRO)
    if not allowed:
        _log_hard_blocked(epic, ExecutionPath.MICRO, reason)
        return False
    return True


def hard_guard_path_b_handoff(epic: str) -> bool:
    allowed, reason = is_path_hard_allowed(epic, ExecutionPath.PATH_B_HANDOFF)
    if not allowed:
        _log_hard_blocked(epic, ExecutionPath.PATH_B_HANDOFF, reason)
        return False
    return True
