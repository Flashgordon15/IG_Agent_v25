"""
Daily P&L targeting engine — Phase 9 advisory progress and bias recommendations (v39).

Helps the agent understand daily points/P&L progress and advisory bias adjustments.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from runtime.regime_detection import MarketRegime

_OVERRIDE: dict[str, Any] | None = None

DEFAULT_TARGET_POINTS = 1000
_GBP_TO_POINTS = 10.0


@dataclass
class DailyPnlTargeting:
    target_points: int
    current_points: int
    progress_ratio: float
    recommended_bias: dict[str, Any]
    bias_confidence: int
    bias_reason: str
    bias_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_points": int(self.target_points),
            "current_points": int(self.current_points),
            "progress_ratio": round(float(self.progress_ratio), 4),
            "recommended_bias": dict(self.recommended_bias),
            "bias_confidence": int(self.bias_confidence),
            "bias_reason": self.bias_reason,
            "bias_flags": sorted(set(self.bias_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_daily_pnl_targeting_for_tests() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def set_daily_pnl_targeting_for_tests(payload: dict[str, Any] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = payload


def _target_points() -> int:
    raw = os.environ.get("DAILY_PNL_TARGET_POINTS", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_TARGET_POINTS


def _current_points(session_review: dict[str, Any] | None) -> int:
    summary = (session_review or {}).get("session_summary") or {}
    points = summary.get("points_summary") or {}
    combined_gbp = float(points.get("combined_pnl_gbp") or 0)
    closed_gbp = float(points.get("closed_pnl_gbp") or 0)
    wins = int(points.get("closed_wins") or 0)
    # Advisory points proxy: GBP contribution + win count bonus
    raw = max(0.0, combined_gbp) * _GBP_TO_POINTS + wins * 15.0
    return max(0, int(round(raw)))


def _progress_band(ratio: float) -> str:
    if ratio >= 0.75:
        return "ahead"
    if ratio >= 0.40:
        return "on_track"
    if ratio >= 0.20:
        return "behind"
    return "far_behind"


def _dominant_strategy(selector_rows: list[dict[str, Any]] | None) -> str:
    if not selector_rows:
        return "MOMENTUM"
    best = selector_rows[0]
    best_conf = -1
    for row in selector_rows:
        try:
            conf = int(row.get("selector_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        if conf >= best_conf:
            best_conf = conf
            best = row
    return str(best.get("recommended_profile") or "MOMENTUM").upper()


def _avg_sizing_factor(sizing_rows: list[dict[str, Any]] | None) -> float:
    rows = sizing_rows or []
    if not rows:
        return 0.25
    values = [float(r.get("recommended_size_factor") or 0) for r in rows]
    return sum(values) / len(values) if values else 0.25


def _dominant_risk(risk_rows: list[dict[str, Any]] | None) -> str:
    rows = risk_rows or []
    if not rows:
        return "MEDIUM"
    order = {"ZERO": 0, "TIGHT": 1, "MEDIUM": 2, "WIDE": 3, "STRUCTURAL": 4}
    profiles = [str(r.get("risk_profile") or "MEDIUM").upper() for r in rows]
    return min(profiles, key=lambda p: order.get(p, 2))


def _dominant_regime(regime_rows: list[dict[str, Any]] | None) -> str:
    if not regime_rows:
        return MarketRegime.UNKNOWN.value
    return str(regime_rows[0].get("regime_classification") or MarketRegime.UNKNOWN.value).upper()


def _regime_supports_sizing_up(regime: str) -> bool:
    return regime in (
        MarketRegime.TREND.value,
        MarketRegime.LOW_VOL.value,
        MarketRegime.REVERSAL.value,
    )


def _compute_biases(
    *,
    band: str,
    strategy: str,
    avg_size: float,
    risk_profile: str,
    regime: str,
    session_risk: int,
    hard_active_count: int,
) -> tuple[dict[str, Any], list[str], list[str]]:
    flags: list[str] = []
    reasons: list[str] = []

    strategy_bias = strategy
    sizing_bias = 0.0
    risk_bias = 0.0
    frequency_bias = 0.0
    stand_down_bias = 0.1

    if band == "ahead":
        sizing_bias = -0.20
        risk_bias = -0.25
        frequency_bias = -0.20
        stand_down_bias = 0.35
        flags.append("AHEAD_OF_TARGET_PROTECTION")
        reasons.append("progress ≥75% — reduce aggressiveness to protect gains")

    elif band == "on_track":
        sizing_bias = 0.05 if _regime_supports_sizing_up(regime) else 0.0
        risk_bias = 0.0
        frequency_bias = 0.0
        stand_down_bias = 0.10
        flags.append("ON_TRACK_STABLE")
        reasons.append("progress 40–75% — maintain stable bias")

    elif band == "behind":
        sizing_bias = 0.15
        risk_bias = 0.15
        frequency_bias = 0.20
        stand_down_bias = 0.05
        flags.append("BEHIND_TARGET_AGGRESSIVE")
        reasons.append("progress 20–40% — increase aggressiveness")

    else:  # far_behind
        sizing_bias = 0.30
        risk_bias = 0.30
        frequency_bias = 0.35
        stand_down_bias = 0.0
        flags.append("FAR_BEHIND_AGGRESSIVE")
        reasons.append("progress <20% — strong aggressiveness bias")

    # Session risk shaping
    if session_risk >= 60:
        sizing_bias = min(sizing_bias, sizing_bias - 0.10)
        risk_bias -= 0.10
        stand_down_bias = min(1.0, stand_down_bias + 0.15)
        flags.append("SESSION_RISK_BIAS_TIGHTEN")

    # Hard enforcement dampening
    if hard_active_count > 0:
        sizing_bias = min(sizing_bias, 0.0)
        frequency_bias = min(frequency_bias, 0.0)
        stand_down_bias = min(1.0, stand_down_bias + 0.10 * hard_active_count)
        flags.append("ENFORCEMENT_BIAS_DAMPEN")

    # Risk profile context
    if risk_profile in ("TIGHT", "ZERO") and sizing_bias > 0:
        sizing_bias *= 0.7
        flags.append("TIGHT_RISK_SIZING_CAP")

    if band == "on_track" and _regime_supports_sizing_up(regime) and sizing_bias > 0:
        flags.append("REGIME_SUPPORTED_SIZING_UP")

    recommended = {
        "strategy_bias": strategy_bias,
        "sizing_bias": round(max(-0.5, min(0.5, sizing_bias)), 4),
        "risk_bias": round(max(-0.5, min(0.5, risk_bias)), 4),
        "frequency_bias": round(max(-0.5, min(0.5, frequency_bias)), 4),
        "stand_down_bias": round(max(0.0, min(1.0, stand_down_bias)), 4),
        "reference_size_factor": round(avg_size, 4),
    }
    return recommended, flags, reasons


def _compute_bias_confidence(
    *,
    progress_ratio: float,
    quality: int,
    stability: int,
    selector_confidence: int,
    sizing_confidence: int,
    risk_confidence: int,
) -> int:
    progress_component = min(100, int(progress_ratio * 100))
    raw = (
        progress_component * 0.25
        + quality * 0.20
        + stability * 0.15
        + selector_confidence * 0.20
        + sizing_confidence * 0.10
        + risk_confidence * 0.10
    )
    return max(0, min(100, int(round(raw))))


def build_daily_pnl_targeting(
    *,
    session_review: dict[str, Any] | None = None,
    regime_aware_strategy_selector: list[dict[str, Any]] | None = None,
    regime_risk_envelope: list[dict[str, Any]] | None = None,
    regime_sizing_advice: list[dict[str, Any]] | None = None,
    strategy_performance_memory: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    regime_detection: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build session-level daily P&L targeting advisory payload."""
    if _OVERRIDE is not None:
        return dict(_OVERRIDE)

    target = _target_points()
    current = _current_points(session_review)
    progress = min(1.0, current / target) if target > 0 else 0.0
    band = _progress_band(progress)

    quality = int((session_review or {}).get("session_quality_score") or 50)
    risk = int((session_review or {}).get("session_risk_score") or 30)
    stability = int((session_review or {}).get("session_stability_score") or 50)

    strategy = _dominant_strategy(regime_aware_strategy_selector)
    avg_size = _avg_sizing_factor(regime_sizing_advice)
    risk_profile = _dominant_risk(regime_risk_envelope)
    regime = _dominant_regime(regime_detection)

    hard_active = sum(1 for r in (hard_enforcement_decisions or []) if r.get("active"))

    recommended_bias, flags, reasons = _compute_biases(
        band=band,
        strategy=strategy,
        avg_size=avg_size,
        risk_profile=risk_profile,
        regime=regime,
        session_risk=risk,
        hard_active_count=hard_active,
    )

    selector_conf = 50
    if regime_aware_strategy_selector:
        try:
            selector_conf = max(int(r.get("selector_confidence") or 0) for r in regime_aware_strategy_selector)
        except ValueError:
            pass

    sizing_conf = 50
    if regime_sizing_advice:
        try:
            sizing_conf = int(
                sum(int(r.get("sizing_confidence") or 0) for r in regime_sizing_advice) / len(regime_sizing_advice)
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    risk_conf = 50
    if regime_risk_envelope:
        try:
            risk_conf = int(
                sum(int(r.get("risk_confidence") or 0) for r in regime_risk_envelope) / len(regime_risk_envelope)
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    perf_win = (strategy_performance_memory or {}).get("win_rates") or {}
    confidence = _compute_bias_confidence(
        progress_ratio=progress,
        quality=quality,
        stability=stability,
        selector_confidence=selector_conf,
        sizing_confidence=sizing_conf,
        risk_confidence=risk_conf,
    )

    if (adaptive_thresholds or {}).get("adjustment_flags"):
        flags.append("THRESHOLD_CONTEXT_APPLIED")

    factors = {
        "session_progress": {
            "band": band,
            "current_points": current,
            "target_points": target,
        },
        "regime": regime,
        "performance": perf_win,
        "thresholds": (adaptive_thresholds or {}).get("adjustment_flags") or [],
        "risk_envelope": risk_profile,
        "sizing_state": {"avg_size_factor": round(avg_size, 4), "epics": len(regime_sizing_advice or [])},
        "enforcement_state": {"active_epics": hard_active},
    }

    result = DailyPnlTargeting(
        target_points=target,
        current_points=current,
        progress_ratio=progress,
        recommended_bias=recommended_bias,
        bias_confidence=confidence,
        bias_reason="; ".join(reasons),
        bias_flags=flags,
        contributing_factors=factors,
    )
    return result.to_dict()
