"""
Regime-aware strategy selector — Phase 6 unified advisory recommendations (v36).

Merges regime detection, performance memory, adaptive thresholds, and session
intelligence into predictive strategy recommendations. Advisory-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.adaptive_thresholds import BASELINE_THRESHOLDS
from runtime.strategy_controller import ExecutionPath

_PROFILES = ("SCALP", "MOMENTUM", "SWING", "ROTATION", "STAND_DOWN")
_PROFILE_PATHS: dict[str, list[str]] = {
    "SCALP": [ExecutionPath.MICRO.value],
    "MOMENTUM": [ExecutionPath.PATH_A.value],
    "SWING": [ExecutionPath.PATH_A.value],
    "ROTATION": [ExecutionPath.PATH_B_HANDOFF.value],
    "STAND_DOWN": [],
}

_REGIME_CONFIDENCE_PRIMARY = 70
_PERFORMANCE_BIAS_MIN = 60
_TRANSITION_OVERRIDE = 85
_HIGH_QUALITY = 75
_HIGH_RISK = 60

_OVERRIDE: list[dict[str, Any]] | None = None


@dataclass
class RegimeAwareSelection:
    epic: str
    recommended_profile: str
    selector_confidence: int
    selector_reason: str
    selector_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "recommended_profile": self.recommended_profile,
            "selector_confidence": int(self.selector_confidence),
            "selector_reason": self.selector_reason,
            "selector_flags": sorted(set(self.selector_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_regime_aware_selector_for_tests() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def set_regime_aware_selector_for_tests(selections: list[dict[str, Any]] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = selections


def _index_by_epic(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(r["epic"]): r for r in (rows or []) if r.get("epic")}


def _profile_blocked_by_hard(profile: str, hard_row: dict[str, Any] | None) -> bool:
    if not hard_row or not hard_row.get("active"):
        return False
    if profile == "STAND_DOWN":
        return False
    required = _PROFILE_PATHS.get(profile.upper(), [])
    if not required:
        return bool(hard_row.get("hard_block_paths"))
    allowed = set(hard_row.get("hard_allow_paths") or [])
    return not any(path in allowed for path in required)


def _infer_profile_from_hard(hard_row: dict[str, Any] | None) -> str | None:
    if not hard_row or not hard_row.get("active"):
        return None
    allowed = set(hard_row.get("hard_allow_paths") or [])
    if ExecutionPath.MICRO.value in allowed and ExecutionPath.PATH_A.value not in allowed:
        return "SCALP"
    if ExecutionPath.PATH_B_HANDOFF.value in allowed and ExecutionPath.PATH_A.value not in allowed:
        return "ROTATION"
    if ExecutionPath.PATH_A.value in allowed and ExecutionPath.MICRO.value not in allowed:
        return "MOMENTUM"
    if not allowed:
        return "STAND_DOWN"
    return None


def _fallback_profile(
    *,
    blocked: str,
    regime_profile: str | None,
    performance_profile: str | None,
    baseline_profile: str | None,
    hard_row: dict[str, Any] | None,
) -> str:
    for candidate in (regime_profile, performance_profile, baseline_profile):
        if not candidate or candidate.upper() == blocked.upper():
            continue
        if not _profile_blocked_by_hard(candidate.upper(), hard_row):
            return candidate.upper()
    inferred = _infer_profile_from_hard(hard_row)
    if inferred and inferred != blocked.upper():
        return inferred
    return "STAND_DOWN"


def _threshold_confidence_delta(threshold_adjustments: dict[str, Any] | None) -> int:
    if not threshold_adjustments:
        return 0
    delta = 0
    soft = float(threshold_adjustments.get("SOFT_BLOCK_THRESHOLD") or BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"])
    hard = float(threshold_adjustments.get("HARD_BLOCK_THRESHOLD") or BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"])
    scalp = float(
        threshold_adjustments.get("SCALP_CONFIDENCE_THRESHOLD") or BASELINE_THRESHOLDS["SCALP_CONFIDENCE_THRESHOLD"]
    )
    if soft < BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"]:
        delta += 5
    if hard > BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"]:
        delta -= 5
    if scalp < BASELINE_THRESHOLDS["SCALP_CONFIDENCE_THRESHOLD"]:
        delta += 3
    return delta


def _threshold_summary(threshold_adjustments: dict[str, Any] | None) -> str:
    delta = _threshold_confidence_delta(threshold_adjustments)
    if delta > 0:
        return "loosened"
    if delta < 0:
        return "tightened"
    return "neutral"


def _apply_session_safety(
    profile: str,
    confidence: int,
    *,
    quality: int,
    risk: int,
    flags: list[str],
    reasons: list[str],
) -> tuple[str, int]:
    current = profile.upper()
    if risk >= _HIGH_RISK and current in ("SCALP", "MOMENTUM"):
        profile = "SWING" if current == "MOMENTUM" else "ROTATION"
        confidence = max(40, confidence - 10)
        flags.append("SESSION_SAFETY_ADJUST")
        reasons.append(f"high session risk {risk} — shifted from {current} toward {profile}")
    elif quality >= _HIGH_QUALITY and current in ("SWING", "ROTATION", "STAND_DOWN"):
        if risk < 45:
            profile = "MOMENTUM" if current in ("SWING", "ROTATION") else current
            confidence = min(95, confidence + 8)
            flags.append("SESSION_SAFETY_ADJUST")
            reasons.append(f"high session quality {quality} — aggressive profile permitted")
    return profile, confidence


def decide_epic_regime_aware_selection(
    epic: str,
    *,
    regime_row: dict[str, Any] | None = None,
    alignment_row: dict[str, Any] | None = None,
    performance_memory: dict[str, Any] | None = None,
    weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    selector_row: dict[str, Any] | None = None,
    transition_row: dict[str, Any] | None = None,
    hard_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unified regime-aware strategy recommendation for one epic."""
    flags: list[str] = []
    reasons: list[str] = []

    baseline = str((selector_row or {}).get("recommended_strategy_profile") or "MOMENTUM").upper()
    try:
        baseline_conf = int((selector_row or {}).get("confidence") or 50)
    except (TypeError, ValueError):
        baseline_conf = 50

    regime_profile = str((alignment_row or {}).get("recommended_profile") or "").upper() or None
    try:
        regime_conf = int((regime_row or {}).get("regime_confidence") or 0)
    except (TypeError, ValueError):
        regime_conf = 0

    perf_profile = str((weighting_advice or {}).get("recommended_bias") or "").upper() or None
    try:
        perf_conf = int((weighting_advice or {}).get("bias_confidence") or 0)
    except (TypeError, ValueError):
        perf_conf = 0

    try:
        transition_conf = int((transition_row or {}).get("transition_confidence") or 0)
    except (TypeError, ValueError):
        transition_conf = 0
    target_profile = str((transition_row or {}).get("target_profile") or "").upper()
    current_profile = str((transition_row or {}).get("current_profile") or "").upper()

    quality = int((session_review or {}).get("session_quality_score") or 50)
    risk = int((session_review or {}).get("session_risk_score") or 30)
    stability = int((session_review or {}).get("session_stability_score") or 50)

    threshold_adj = (adaptive_thresholds or {}).get("threshold_adjustments") or {}
    conf_delta = _threshold_confidence_delta(threshold_adj)
    if conf_delta != 0:
        flags.append("THRESHOLD_ADJUSTMENT_APPLIED")

    profile = baseline
    confidence = baseline_conf
    reasons.append(f"baseline selector recommends {baseline}")

    # 1. Regime alignment (primary when confident)
    if regime_conf >= _REGIME_CONFIDENCE_PRIMARY and regime_profile:
        profile = regime_profile
        confidence = max(confidence, min(95, (regime_conf + int((alignment_row or {}).get("alignment_confidence") or regime_conf)) // 2))
        flags.append("REGIME_ALIGNMENT_PRIMARY")
        reasons.append(f"regime confidence {regime_conf}≥{_REGIME_CONFIDENCE_PRIMARY} — {regime_profile}")

    # 5. Transition override (highest priority when very confident)
    if transition_conf >= _TRANSITION_OVERRIDE and target_profile and target_profile != current_profile:
        profile = target_profile
        confidence = max(confidence, min(95, transition_conf))
        flags.append("TRANSITION_OVERRIDES")
        reasons.append(f"transition confidence {transition_conf}≥{_TRANSITION_OVERRIDE} — target {target_profile}")

    # 2. Performance memory bias (secondary blend)
    if perf_conf >= _PERFORMANCE_BIAS_MIN and perf_profile:
        if profile != perf_profile:
            blended_conf = (confidence + perf_conf) // 2
            if perf_conf >= regime_conf or regime_conf < _REGIME_CONFIDENCE_PRIMARY:
                profile = perf_profile
                confidence = blended_conf
                flags.append("PERFORMANCE_BIAS_SECONDARY")
                reasons.append(f"performance bias {perf_profile} ({perf_conf}%) blended")
            else:
                confidence = min(95, confidence + perf_conf // 10)
                flags.append("PERFORMANCE_BIAS_SECONDARY")
                reasons.append(f"performance bias {perf_profile} reinforces confidence")
        else:
            confidence = min(95, confidence + 5)
            flags.append("PERFORMANCE_BIAS_SECONDARY")

    confidence = max(25, min(95, confidence + conf_delta))

    # 4. Session safety shaping
    profile, confidence = _apply_session_safety(
        profile, confidence, quality=quality, risk=risk, flags=flags, reasons=reasons
    )

    # 6. Hard enforcement fallback
    if _profile_blocked_by_hard(profile, hard_row):
        blocked = profile
        profile = _fallback_profile(
            blocked=blocked,
            regime_profile=regime_profile,
            performance_profile=perf_profile,
            baseline_profile=baseline,
            hard_row=hard_row,
        )
        flags.append("HARD_ENFORCEMENT_FALLBACK")
        reasons.append(f"hard enforcement blocked {blocked} — fallback to {profile}")
        confidence = max(35, confidence - 8)

    transition_state = "stable"
    if transition_row and target_profile and target_profile != current_profile:
        transition_state = f"{current_profile or 'UNKNOWN'}→{target_profile} ({transition_conf}%)"

    factors = {
        "regime_alignment": regime_profile or "none",
        "regime_classification": (regime_row or {}).get("regime_classification"),
        "performance_bias": perf_profile or "none",
        "threshold_adjustments": _threshold_summary(threshold_adj),
        "session_quality": quality,
        "risk_state": risk,
        "session_stability": stability,
        "transition_state": transition_state,
        "baseline_selector": baseline,
        "hard_enforcement_active": bool((hard_row or {}).get("active")),
    }

    selection = RegimeAwareSelection(
        epic=epic,
        recommended_profile=profile,
        selector_confidence=confidence,
        selector_reason="; ".join(reasons[:4]),
        selector_flags=flags,
        contributing_factors=factors,
    )
    return selection.to_dict()


def build_regime_aware_strategy_selector(
    *,
    regime_detection: list[dict[str, Any]] | None = None,
    regime_strategy_alignment: list[dict[str, Any]] | None = None,
    strategy_performance_memory: dict[str, Any] | None = None,
    strategy_weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build regime-aware selector rows for all monitored epics."""
    if _OVERRIDE is not None:
        return list(_OVERRIDE)

    regime_by_epic = _index_by_epic(regime_detection)
    alignment_by_epic = _index_by_epic(regime_strategy_alignment)
    selector_by_epic = _index_by_epic(strategy_selector_advice)
    transition_by_epic = _index_by_epic(strategy_transition_advice)
    hard_by_epic = _index_by_epic(hard_enforcement_decisions)

    epics: list[str] = []
    for row in trade_pipeline_health or []:
        epic = str(row.get("epic") or "")
        if epic and epic not in epics:
            epics.append(epic)
    for epic in regime_by_epic:
        if epic not in epics:
            epics.append(epic)

    return [
        decide_epic_regime_aware_selection(
            epic,
            regime_row=regime_by_epic.get(epic),
            alignment_row=alignment_by_epic.get(epic),
            performance_memory=strategy_performance_memory,
            weighting_advice=strategy_weighting_advice,
            adaptive_thresholds=adaptive_thresholds,
            session_review=session_review,
            selector_row=selector_by_epic.get(epic),
            transition_row=transition_by_epic.get(epic),
            hard_row=hard_by_epic.get(epic),
        )
        for epic in epics
    ]
