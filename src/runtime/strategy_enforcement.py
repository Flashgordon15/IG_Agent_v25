"""
Strategy enforcement engine — Phase 1 soft, non-invasive path gating.

Soft-blocks return early with a log event; no exceptions, no signal/sizing changes.
Does NOT replace strategy_controller hard guards — runs as an additional layer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.strategy_controller import ExecutionPath
from runtime.strategy_selector import CRITICAL_ANOMALIES, _feed_degraded, _governance_for_epic

OWNERSHIP_CONFIDENCE_THRESHOLD = 70
TRANSITION_CONFIDENCE_THRESHOLD = 80
ROTATION_SCALP_SELECTOR_THRESHOLD = 80

_DECISIONS_OVERRIDE: list[dict[str, Any]] | None = None
_DECISIONS_CACHE: dict[str, dict[str, Any]] = {}
_DECISIONS_CACHE_AT: float = 0.0
_DECISIONS_CACHE_TTL_SEC = 1.0

_ALL_PATH_VALUES = [p.value for p in ExecutionPath]

_PROFILE_PATHS: dict[str, list[str]] = {
    "SCALP": [ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value],
    "MOMENTUM": [ExecutionPath.PATH_A.value],
    "SWING": [ExecutionPath.PATH_A.value],
    "ROTATION": [ExecutionPath.PATH_B_HANDOFF.value],
    "STAND_DOWN": [],
}


class EnforcementOwnership(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    STAND_DOWN = "STAND_DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass
class StrategyEnforcementDecision:
    epic: str
    soft_block_paths: list[str]
    soft_allow_paths: list[str]
    enforcement_confidence: int
    enforcement_reason: str
    enforcement_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "soft_block_paths": sorted(set(self.soft_block_paths)),
            "soft_allow_paths": sorted(set(self.soft_allow_paths)),
            "enforcement_confidence": int(self.enforcement_confidence),
            "enforcement_reason": self.enforcement_reason,
            "enforcement_flags": sorted(set(self.enforcement_flags)),
        }


def reset_strategy_enforcement_for_tests() -> None:
    global _DECISIONS_OVERRIDE, _DECISIONS_CACHE, _DECISIONS_CACHE_AT
    _DECISIONS_OVERRIDE = None
    _DECISIONS_CACHE = {}
    _DECISIONS_CACHE_AT = 0.0


def set_strategy_enforcement_decisions_for_tests(decisions: list[dict[str, Any]] | None) -> None:
    global _DECISIONS_OVERRIDE, _DECISIONS_CACHE, _DECISIONS_CACHE_AT
    _DECISIONS_OVERRIDE = decisions
    _DECISIONS_CACHE = {row["epic"]: row for row in (decisions or [])}
    _DECISIONS_CACHE_AT = time.time()


def _parse_ownership(raw: str | None) -> EnforcementOwnership:
    value = str(raw or EnforcementOwnership.UNKNOWN.value).upper()
    try:
        return EnforcementOwnership(value)
    except ValueError:
        return EnforcementOwnership.UNKNOWN


def _selector_scalp_high(selector_advice: dict[str, Any] | None, threshold: int) -> bool:
    if not selector_advice:
        return False
    rec = str(selector_advice.get("recommended_strategy_profile") or "").upper()
    try:
        conf = int(selector_advice.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0
    return rec == "SCALP" and conf >= threshold


def _profile_conflict(
    ownership: EnforcementOwnership,
    selector_advice: dict[str, Any] | None,
) -> bool:
    if not selector_advice or ownership is EnforcementOwnership.UNKNOWN:
        return False
    rec = _parse_ownership(selector_advice.get("recommended_strategy_profile"))
    return rec is not EnforcementOwnership.UNKNOWN and rec.value != ownership.value


def decide_epic_enforcement(
    epic: str,
    *,
    controller_row: dict[str, Any] | None,
    transition_row: dict[str, Any] | None,
    selector_advice: dict[str, Any] | None,
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
) -> StrategyEnforcementDecision:
    """Derive soft enforcement for one epic — observability only."""
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
    soft_block: set[str] = set()
    soft_allow: set[str] = set()
    flags: list[str] = []
    reasons: list[str] = []

    if _feed_degraded(api_feed_health):
        flags.append("FEED_DEGRADED")

    anomalies = set(gov_row.get("pipeline_anomalies") or [])
    anomalies.update(gov_row.get("feed_anomalies") or [])
    if anomalies & CRITICAL_ANOMALIES:
        flags.append("PIPELINE_CRITICAL")

    if _profile_conflict(ownership, selector_advice):
        flags.append("PROFILE_CONFLICT")

    if ownership is EnforcementOwnership.STAND_DOWN:
        soft_block.update(_ALL_PATH_VALUES)
        soft_allow.clear()
        flags.append("STAND_DOWN_ACTIVE")
        reasons.append("STAND_DOWN — all execution paths soft-blocked")

    elif ownership is EnforcementOwnership.SCALP:
        try:
            from system.dual_regime import sb_macro_path_a_carve_active

            _sb_path_a = sb_macro_path_a_carve_active()
        except Exception:
            _sb_path_a = False
        if _sb_path_a:
            soft_allow.add(ExecutionPath.PATH_A.value)
            soft_block.update(
                [ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value]
            )
            flags.append("SCALP_SOFT_ENFORCEMENT")
            flags.append("SB_MACRO_PATH_A_CARVE")
            reasons.append(
                "SCALP ownership — SB macro carve allows Path A; micro soft-blocked"
            )
        else:
            soft_allow.update(_PROFILE_PATHS["SCALP"])
            if enforcement_confidence >= OWNERSHIP_CONFIDENCE_THRESHOLD:
                soft_block.add(ExecutionPath.PATH_A.value)
                flags.append("SCALP_SOFT_ENFORCEMENT")
                reasons.append("SCALP ownership — Path A soft-blocked")

    elif ownership is EnforcementOwnership.MOMENTUM:
        soft_allow.add(ExecutionPath.PATH_A.value)
        if enforcement_confidence >= OWNERSHIP_CONFIDENCE_THRESHOLD:
            soft_block.update([ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value])
            flags.append("MOMENTUM_SOFT_ENFORCEMENT")
            reasons.append("MOMENTUM ownership — micro and Path B handoff soft-blocked")

    elif ownership is EnforcementOwnership.SWING:
        soft_allow.add(ExecutionPath.PATH_A.value)
        soft_block.update([ExecutionPath.MICRO.value, ExecutionPath.PATH_B_HANDOFF.value])
        flags.append("SWING_SOFT_ENFORCEMENT")
        reasons.append("SWING ownership — Path A only")

    elif ownership is EnforcementOwnership.ROTATION:
        soft_allow.add(ExecutionPath.PATH_B_HANDOFF.value)
        soft_block.update([ExecutionPath.PATH_A.value, ExecutionPath.MICRO.value])
        if _selector_scalp_high(selector_advice, ROTATION_SCALP_SELECTOR_THRESHOLD):
            soft_block.discard(ExecutionPath.MICRO.value)
            soft_allow.add(ExecutionPath.MICRO.value)
            flags.append("ROTATION_SCALP_EXCEPTION")
            reasons.append("ROTATION — micro permitted via high-confidence SCALP selector")
        else:
            reasons.append("ROTATION — sweep allowed; execution soft-blocked")

    if transition_row and transition_conf >= TRANSITION_CONFIDENCE_THRESHOLD:
        current = str(transition_row.get("current_profile") or "").upper()
        target = str(transition_row.get("target_profile") or "").upper()
        if current and target and current != target:
            flags.append("HIGH_CONFIDENCE_TRANSITION")
            for path in _PROFILE_PATHS.get(current, []):
                soft_block.add(path)
            for path in _PROFILE_PATHS.get(target, []):
                soft_allow.add(path)
                soft_block.discard(path)
            reasons.append(f"high-confidence transition {current} → {target}")

    if not reasons:
        reasons.append("soft enforcement idle — no active blocks")

    reason = "; ".join(reasons)
    return StrategyEnforcementDecision(
        epic=epic,
        soft_block_paths=sorted(soft_block),
        soft_allow_paths=sorted(soft_allow),
        enforcement_confidence=min(100, enforcement_confidence),
        enforcement_reason=reason,
        enforcement_flags=flags,
    )


def build_strategy_enforcement_decisions(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-epic soft enforcement decisions for GUI and guards."""
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
        decision = decide_epic_enforcement(
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
        build_strategy_enforcement_decisions()
    return _DECISIONS_CACHE.get(epic)


def is_path_soft_allowed(epic: str, path: ExecutionPath | str) -> tuple[bool, str]:
    try:
        import os

        from system.demo_execution_plane import execution_guards_relaxed

        if os.environ.get("IG_AGENT_PYTEST") != "1" and execution_guards_relaxed(
            epic=epic
        ):
            return True, ""
    except Exception:
        pass
    path_value = path.value if isinstance(path, ExecutionPath) else str(path)
    row = _decision_for_epic(str(epic or ""))
    if not row:
        return True, ""
    blocked = set(row.get("soft_block_paths") or [])
    if path_value in blocked:
        return False, str(row.get("enforcement_reason") or "soft_blocked_by_strategy_enforcement")
    return True, ""


def _log_soft_blocked(epic: str, path: ExecutionPath, reason: str) -> None:
    try:
        from system.logging_engine import log_engine

        log_engine(
            f"StrategyEnforcement: soft_blocked_by_strategy_enforcement epic={epic} "
            f"path={path.value} reason={reason}"
        )
    except Exception:
        pass


def soft_guard_path_a_execution(epic: str) -> bool:
    allowed, reason = is_path_soft_allowed(epic, ExecutionPath.PATH_A)
    if not allowed:
        _log_soft_blocked(epic, ExecutionPath.PATH_A, reason)
        return False
    return True


def soft_guard_micro_dispatch(epic: str) -> bool:
    allowed, reason = is_path_soft_allowed(epic, ExecutionPath.MICRO)
    if not allowed:
        _log_soft_blocked(epic, ExecutionPath.MICRO, reason)
        return False
    return True


def soft_guard_path_b_handoff(epic: str) -> bool:
    allowed, reason = is_path_soft_allowed(epic, ExecutionPath.PATH_B_HANDOFF)
    if not allowed:
        _log_soft_blocked(epic, ExecutionPath.PATH_B_HANDOFF, reason)
        return False
    return True
