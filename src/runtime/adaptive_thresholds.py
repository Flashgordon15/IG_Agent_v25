"""
Adaptive threshold engine — Phase 3 self-learning (advisory-only).

Synthesises session review, loosening advice, and self-reflection into
recommended threshold adjustments. Does NOT modify trading, execution, or config.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

BASELINE_THRESHOLDS: dict[str, float] = {
    "SCALP_CONFIDENCE_THRESHOLD": 65.0,
    "MOMENTUM_CONFIDENCE_THRESHOLD": 70.0,
    "SWING_CONFIDENCE_THRESHOLD": 75.0,
    "ROTATION_SCALP_OVERRIDE_THRESHOLD": 85.0,
    "STAND_DOWN_SENSITIVITY": 50.0,
    "TRANSITION_CONFIDENCE_THRESHOLD": 85.0,
    "SOFT_BLOCK_THRESHOLD": 70.0,
    "HARD_BLOCK_THRESHOLD": 85.0,
    "VOLATILITY_GATE_LOW": -1.0,
    "VOLATILITY_GATE_HIGH": 2.5,
    "FEED_HEALTH_GATE": 70.0,
}

_THRESHOLD_BOUNDS: dict[str, tuple[float, float]] = {
    "SCALP_CONFIDENCE_THRESHOLD": (40.0, 90.0),
    "MOMENTUM_CONFIDENCE_THRESHOLD": (45.0, 95.0),
    "SWING_CONFIDENCE_THRESHOLD": (50.0, 95.0),
    "ROTATION_SCALP_OVERRIDE_THRESHOLD": (70.0, 95.0),
    "STAND_DOWN_SENSITIVITY": (20.0, 90.0),
    "TRANSITION_CONFIDENCE_THRESHOLD": (70.0, 95.0),
    "SOFT_BLOCK_THRESHOLD": (50.0, 90.0),
    "HARD_BLOCK_THRESHOLD": (75.0, 95.0),
    "VOLATILITY_GATE_LOW": (-3.0, 0.0),
    "VOLATILITY_GATE_HIGH": (1.0, 4.0),
    "FEED_HEALTH_GATE": (50.0, 95.0),
}

_OVERRIDE: dict[str, Any] | None = None


@dataclass
class AdaptiveThresholds:
    threshold_adjustments: dict[str, float]
    adjustment_reason: str
    adjustment_flags: list[str] = field(default_factory=list)
    adjustment_confidence: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_adjustments": {k: float(v) for k, v in self.threshold_adjustments.items()},
            "adjustment_reason": self.adjustment_reason,
            "adjustment_flags": sorted(set(self.adjustment_flags)),
            "adjustment_confidence": int(self.adjustment_confidence),
        }


def reset_adaptive_thresholds_for_tests() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def set_adaptive_thresholds_for_tests(payload: dict[str, Any] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = payload


def _clamp(key: str, value: float) -> float:
    lo, hi = _THRESHOLD_BOUNDS.get(key, (0.0, 100.0))
    return max(lo, min(hi, value))


def _baseline_copy() -> dict[str, float]:
    return copy.deepcopy(BASELINE_THRESHOLDS)


def _session_flags(session_review: dict[str, Any] | None) -> set[str]:
    return set((session_review or {}).get("session_flags") or [])


def _reflection_flags(self_reflection: dict[str, Any] | None) -> set[str]:
    return set((self_reflection or {}).get("reflection_flags") or [])


def _quality(session_review: dict[str, Any] | None) -> int:
    try:
        return int((session_review or {}).get("session_quality_score") or 0)
    except (TypeError, ValueError):
        return 0


def _risk(session_review: dict[str, Any] | None) -> int:
    try:
        return int((session_review or {}).get("session_risk_score") or 0)
    except (TypeError, ValueError):
        return 0


def _stability(session_review: dict[str, Any] | None) -> int:
    try:
        return int((session_review or {}).get("session_stability_score") or 0)
    except (TypeError, ValueError):
        return 0


def _loosening_confidence(loosening_advice: dict[str, Any] | None) -> int:
    try:
        return int((loosening_advice or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def _missed_opportunity_active(reflection_flags: set[str]) -> bool:
    return bool(
        reflection_flags.intersection({"MISSED_OPPORTUNITY", "MISSED_PNL_OPPORTUNITY"})
        or reflection_flags & {f for f in reflection_flags if "MISSED" in f and "OPPORTUNITY" in f}
    )


def build_adaptive_thresholds(
    *,
    session_review: dict[str, Any] | None = None,
    loosening_advice: dict[str, Any] | None = None,
    self_reflection: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    strategy_controller_decisions: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Advisory threshold synthesis from session intelligence layers."""
    if _OVERRIDE is not None:
        return dict(_OVERRIDE)

    thresholds = _baseline_copy()
    flags: list[str] = []
    reasons: list[str] = []

    session_flags = _session_flags(session_review)
    reflection_flags = _reflection_flags(self_reflection)
    quality = _quality(session_review)
    risk = _risk(session_review)
    stability = _stability(session_review)
    loosening_conf = _loosening_confidence(loosening_advice)

    # Under-trading — lower entry confidence and soft-block bar
    if "UNDER_TRADING" in session_flags:
        thresholds["SCALP_CONFIDENCE_THRESHOLD"] -= 5.0
        thresholds["MOMENTUM_CONFIDENCE_THRESHOLD"] -= 5.0
        thresholds["SOFT_BLOCK_THRESHOLD"] -= 5.0
        flags.append("UNDER_TRADING_ADJUST")
        reasons.append("UNDER_TRADING: lowered scalp/momentum confidence and soft-block threshold")

    # Over-blocking — raise hard-block bar, reduce stand-down sensitivity
    if "OVER_BLOCKING_AGGRESSIVE" in session_flags:
        thresholds["HARD_BLOCK_THRESHOLD"] += 5.0
        thresholds["STAND_DOWN_SENSITIVITY"] -= 5.0
        flags.append("OVER_BLOCKING_ADJUST")
        reasons.append("OVER_BLOCKING_AGGRESSIVE: raised hard-block threshold, reduced stand-down sensitivity")

    # High-quality session — loosen gates
    if quality >= 75:
        thresholds["VOLATILITY_GATE_LOW"] -= 0.2
        thresholds["VOLATILITY_GATE_HIGH"] += 0.2
        thresholds["FEED_HEALTH_GATE"] -= 5.0
        thresholds["TRANSITION_CONFIDENCE_THRESHOLD"] -= 3.0
        flags.append("HIGH_QUALITY_SESSION")
        reasons.append(f"session_quality_score {quality}≥75: loosened volatility, feed, and transition gates")

    # High-risk session — tighten gates
    if risk >= 60:
        thresholds["VOLATILITY_GATE_LOW"] += 0.2
        thresholds["VOLATILITY_GATE_HIGH"] -= 0.2
        thresholds["FEED_HEALTH_GATE"] += 5.0
        thresholds["STAND_DOWN_SENSITIVITY"] += 5.0
        flags.append("HIGH_RISK_SESSION")
        reasons.append(f"session_risk_score {risk}≥60: tightened volatility, feed gates; raised stand-down sensitivity")

    # Strategy misalignment — selector up, enforcement down
    if "SELECTOR_ENFORCEMENT_CONFLICT" in reflection_flags:
        thresholds["SCALP_CONFIDENCE_THRESHOLD"] += 3.0
        thresholds["MOMENTUM_CONFIDENCE_THRESHOLD"] += 3.0
        thresholds["SWING_CONFIDENCE_THRESHOLD"] += 3.0
        thresholds["SOFT_BLOCK_THRESHOLD"] -= 3.0
        thresholds["HARD_BLOCK_THRESHOLD"] -= 3.0
        flags.append("STRATEGY_ALIGNMENT_ADJUST")
        reasons.append("SELECTOR_ENFORCEMENT_CONFLICT: raised selector thresholds, lowered enforcement thresholds")

    # Missed opportunities — lower confidence bars
    if _missed_opportunity_active(reflection_flags):
        thresholds["SCALP_CONFIDENCE_THRESHOLD"] -= 5.0
        thresholds["MOMENTUM_CONFIDENCE_THRESHOLD"] -= 5.0
        flags.append("MISSED_OPPORTUNITY_ADJUST")
        reasons.append("missed opportunity reflection: lowered scalp and momentum confidence thresholds")

    # Drawdown protection
    if "DRAWDOWN_HIGH" in session_flags:
        thresholds["STAND_DOWN_SENSITIVITY"] += 5.0
        thresholds["HARD_BLOCK_THRESHOLD"] += 5.0
        flags.append("DRAWDOWN_PROTECTION")
        reasons.append("DRAWDOWN_HIGH: raised stand-down sensitivity and hard-block threshold")

    # Loosening advisor reinforcement (advisory weight only)
    if loosening_conf >= 70 and "HIGH_QUALITY_LOW_RISK" in set((loosening_advice or {}).get("loosening_flags") or []):
        thresholds["SOFT_BLOCK_THRESHOLD"] -= 2.0
        if "HIGH_QUALITY_SESSION" not in flags:
            flags.append("LOOSENING_ADVISOR_REINFORCE")
            reasons.append("high-confidence loosening advice reinforces softer gating")

    # Hard enforcement activity — slight tightening advisory when many epics active
    active_hard = sum(1 for row in (hard_enforcement_decisions or []) if row.get("active"))
    if active_hard >= 2:
        thresholds["HARD_BLOCK_THRESHOLD"] = max(
            thresholds["HARD_BLOCK_THRESHOLD"],
            BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"],
        )
        if "HARD_ENFORCEMENT_ACTIVE" not in flags:
            flags.append("HARD_ENFORCEMENT_ACTIVE")
            reasons.append(f"{active_hard} epics under hard enforcement — maintain elevated hard-block floor")

    for key in thresholds:
        thresholds[key] = _clamp(key, thresholds[key])

    confidence = min(
        100,
        max(
            35,
            (quality + stability) // 2
            + len(flags) * 4
            + (loosening_conf // 10),
        ),
    )

    if not reasons:
        reasons.append("baseline thresholds — no adaptive adjustments triggered")

    result = AdaptiveThresholds(
        threshold_adjustments=thresholds,
        adjustment_reason="; ".join(reasons),
        adjustment_flags=flags,
        adjustment_confidence=confidence,
    )
    return result.to_dict()
