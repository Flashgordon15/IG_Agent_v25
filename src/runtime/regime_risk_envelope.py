"""
Regime-aware risk envelope — Phase 7 advisory risk profiling (v37).

Produces adaptive, regime-aware risk envelopes per epic. Advisory-only.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.adaptive_thresholds import BASELINE_THRESHOLDS
from runtime.regime_detection import MarketRegime

_OVERRIDE: list[dict[str, Any]] | None = None

_RISK_ORDER = ("ZERO", "TIGHT", "MEDIUM", "WIDE", "STRUCTURAL")

_BASE_ENVELOPE: dict[str, str] = {
    "SCALP": "TIGHT",
    "MOMENTUM": "MEDIUM",
    "SWING": "WIDE",
    "ROTATION": "STRUCTURAL",
    "STAND_DOWN": "ZERO",
}

_PROFILE_FLAGS: dict[str, str] = {
    "SCALP": "SCALP_TIGHT_RISK",
    "MOMENTUM": "MOMENTUM_MEDIUM_RISK",
    "SWING": "SWING_WIDE_RISK",
    "ROTATION": "ROTATION_STRUCTURAL_RISK",
    "STAND_DOWN": "STAND_DOWN_ZERO_RISK",
}

_HIGH_SESSION_RISK = 60
_LOW_SESSION_RISK = 35
_HIGH_QUALITY = 75


class RiskProfile(str, Enum):
    TIGHT = "TIGHT"
    MEDIUM = "MEDIUM"
    WIDE = "WIDE"
    STRUCTURAL = "STRUCTURAL"
    ZERO = "ZERO"


@dataclass
class RegimeRiskEnvelope:
    epic: str
    risk_profile: str
    risk_confidence: int
    risk_reason: str
    risk_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "risk_profile": self.risk_profile,
            "risk_confidence": int(self.risk_confidence),
            "risk_reason": self.risk_reason,
            "risk_flags": sorted(set(self.risk_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_regime_risk_envelope_for_tests() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def set_regime_risk_envelope_for_tests(envelopes: list[dict[str, Any]] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = envelopes


def _index_by_epic(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(r["epic"]): r for r in (rows or []) if r.get("epic")}


def _risk_index(profile: str) -> int:
    try:
        return _RISK_ORDER.index(profile.upper())
    except ValueError:
        return _RISK_ORDER.index("MEDIUM")


def _shift_risk(profile: str, *, steps: int, floor: str = "TIGHT", ceiling: str = "STRUCTURAL") -> str:
    idx = _risk_index(profile)
    floor_i = _risk_index(floor)
    ceiling_i = _risk_index(ceiling)
    new_idx = max(floor_i, min(ceiling_i, idx + steps))
    return _RISK_ORDER[new_idx]


def _win_rate_for_profile(performance_memory: dict[str, Any] | None, profile: str) -> float:
    win_rates = (performance_memory or {}).get("win_rates") or {}
    key = f"{profile.lower()}_win_rate"
    try:
        return float(win_rates.get(key) or 50.0)
    except (TypeError, ValueError):
        return 50.0


def _threshold_effect(adaptive_thresholds: dict[str, Any] | None) -> float:
    """Return 0–100 contribution for threshold adjustment effect."""
    adj = (adaptive_thresholds or {}).get("threshold_adjustments") or {}
    if not adj:
        return 50.0
    score = 50.0
    soft = float(adj.get("SOFT_BLOCK_THRESHOLD") or BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"])
    hard = float(adj.get("HARD_BLOCK_THRESHOLD") or BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"])
    if soft < BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"]:
        score += 15.0
    if hard > BASELINE_THRESHOLDS["HARD_BLOCK_THRESHOLD"]:
        score -= 15.0
    stand = float(adj.get("STAND_DOWN_SENSITIVITY") or BASELINE_THRESHOLDS["STAND_DOWN_SENSITIVITY"])
    if stand > BASELINE_THRESHOLDS["STAND_DOWN_SENSITIVITY"]:
        score -= 10.0
    return max(0.0, min(100.0, score))


def _compute_risk_confidence(
    *,
    regime_confidence: int,
    selector_confidence: int,
    performance_confidence: int,
    session_risk: int,
    session_stability: int,
    threshold_effect: float,
) -> int:
    session_component = ((100 - session_risk) * 0.5 + session_stability * 0.5)
    raw = (
        regime_confidence * 0.35
        + selector_confidence * 0.25
        + performance_confidence * 0.20
        + session_component * 0.10
        + threshold_effect * 0.10
    )
    return max(0, min(100, int(round(raw))))


def _resolve_strategy_profile(
    *,
    selector_row: dict[str, Any] | None,
    alignment_row: dict[str, Any] | None,
    hard_row: dict[str, Any] | None,
) -> str:
    if hard_row and hard_row.get("active"):
        flags = set(hard_row.get("enforcement_flags") or [])
        if "STAND_DOWN_HARD" in flags or not hard_row.get("hard_allow_paths"):
            return "STAND_DOWN"
    profile = str((selector_row or {}).get("recommended_profile") or "").upper()
    if profile in _BASE_ENVELOPE:
        return profile
    profile = str((alignment_row or {}).get("recommended_profile") or "").upper()
    if profile in _BASE_ENVELOPE:
        return profile
    return "MOMENTUM"


def decide_epic_risk_envelope(
    epic: str,
    *,
    regime_row: dict[str, Any] | None = None,
    alignment_row: dict[str, Any] | None = None,
    performance_memory: dict[str, Any] | None = None,
    weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    selector_row: dict[str, Any] | None = None,
    hard_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive regime-aware risk envelope for one epic."""
    flags: list[str] = []
    reasons: list[str] = []

    strategy = _resolve_strategy_profile(
        selector_row=selector_row,
        alignment_row=alignment_row,
        hard_row=hard_row,
    )
    regime_class = str((regime_row or {}).get("regime_classification") or MarketRegime.UNKNOWN.value).upper()
    try:
        regime_conf = int((regime_row or {}).get("regime_confidence") or 0)
    except (TypeError, ValueError):
        regime_conf = 0
    try:
        selector_conf = int((selector_row or {}).get("selector_confidence") or 0)
    except (TypeError, ValueError):
        selector_conf = 50
    try:
        perf_conf = int((weighting_advice or {}).get("bias_confidence") or 0)
    except (TypeError, ValueError):
        perf_conf = 50

    quality = int((session_review or {}).get("session_quality_score") or 50)
    risk = int((session_review or {}).get("session_risk_score") or 30)
    stability = int((session_review or {}).get("session_stability_score") or 50)
    session_flags = set((session_review or {}).get("session_flags") or [])
    drawdown = ((session_review or {}).get("session_summary") or {}).get("drawdown_summary") or {}
    try:
        dd_pct = float(drawdown.get("max_drawdown_pct") or 0)
    except (TypeError, ValueError):
        dd_pct = 0.0

    win_rate = _win_rate_for_profile(performance_memory, strategy)
    thresh_effect = _threshold_effect(adaptive_thresholds)

    risk_profile = _BASE_ENVELOPE.get(strategy, "MEDIUM")
    flags.append(_PROFILE_FLAGS.get(strategy, "MOMENTUM_MEDIUM_RISK"))
    reasons.append(f"{strategy} base envelope → {risk_profile}")

    # Profile-specific regime adjustments
    if strategy == "SCALP":
        if regime_class in (MarketRegime.EXTREME_VOL.value, MarketRegime.BREAKOUT.value):
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="TIGHT")
            flags.append("SCALP_TIGHTER_VOL")
            reasons.append("high volatility — tighter SCALP envelope")
        if regime_class == MarketRegime.LIQUIDITY_DROP.value or "FEED_DEGRADED" in session_flags:
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="TIGHT")
            flags.append("SCALP_TIGHTER_FEED")
            reasons.append("degraded feed/liquidity — tighter SCALP envelope")
        if risk >= _HIGH_SESSION_RISK:
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="TIGHT")
            flags.append("SCALP_TIGHTER_SESSION_RISK")
            reasons.append("high session risk — tighter SCALP envelope")

    elif strategy == "MOMENTUM":
        if regime_class == MarketRegime.TREND.value and win_rate >= 55:
            risk_profile = _shift_risk(risk_profile, steps=1, ceiling="WIDE")
            flags.append("MOMENTUM_WIDER_TREND")
            reasons.append("strong trend + performance — wider MOMENTUM envelope")
        if regime_class in (MarketRegime.CHOP.value, MarketRegime.EXTREME_VOL.value):
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="TIGHT")
            flags.append("MOMENTUM_TIGHTER_CHOP_VOL")
            reasons.append("chop/extreme vol — tighter MOMENTUM envelope")

    elif strategy == "SWING":
        if regime_class == MarketRegime.LOW_VOL.value and win_rate >= 55:
            risk_profile = _shift_risk(risk_profile, steps=1, ceiling="WIDE")
            flags.append("SWING_WIDER_LOW_VOL")
            reasons.append("low volatility + performance — wider SWING envelope")
        if regime_class in (MarketRegime.EXTREME_VOL.value, MarketRegime.LIQUIDITY_DROP.value):
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="MEDIUM")
            flags.append("SWING_TIGHTER_EXTREME_LIQUIDITY")
            reasons.append("extreme vol/liquidity drop — tighter SWING envelope")

    elif strategy == "ROTATION":
        if regime_class == MarketRegime.REVERSAL.value and win_rate >= 50:
            risk_profile = _shift_risk(risk_profile, steps=1, ceiling="STRUCTURAL")
            flags.append("ROTATION_WIDER_REVERSAL")
            reasons.append("reversal regime — wider ROTATION envelope")
        if regime_class in (MarketRegime.BREAKOUT.value, MarketRegime.EXTREME_VOL.value):
            risk_profile = _shift_risk(risk_profile, steps=-1, floor="MEDIUM")
            flags.append("ROTATION_TIGHTER_BREAKOUT")
            reasons.append("breakout/extreme vol — tighter ROTATION envelope")

    elif strategy == "STAND_DOWN":
        risk_profile = RiskProfile.ZERO.value
        flags.append("STAND_DOWN_ZERO_RISK")

    if dd_pct >= 5.0:
        risk_profile = _shift_risk(risk_profile, steps=-1, floor="ZERO" if strategy == "STAND_DOWN" else "TIGHT")
        flags.append("DRAWDOWN_TIGHTEN")
        reasons.append(f"drawdown {dd_pct:.1f}% — envelope tightened")

    if hard_row and hard_row.get("active"):
        flags.append("HARD_ENFORCEMENT_ACTIVE")

    confidence = _compute_risk_confidence(
        regime_confidence=regime_conf,
        selector_confidence=selector_conf,
        performance_confidence=perf_conf if perf_conf else int(win_rate),
        session_risk=risk,
        session_stability=stability,
        threshold_effect=thresh_effect,
    )

    factors = {
        "regime": regime_class,
        "strategy": strategy,
        "performance_win_rate": round(win_rate, 2),
        "thresholds": (adaptive_thresholds or {}).get("adjustment_flags") or [],
        "session_risk": risk,
        "session_quality": quality,
        "session_stability": stability,
        "enforcement_state": "active" if (hard_row or {}).get("active") else "idle",
    }

    envelope = RegimeRiskEnvelope(
        epic=epic,
        risk_profile=risk_profile,
        risk_confidence=confidence,
        risk_reason="; ".join(reasons[:4]),
        risk_flags=flags,
        contributing_factors=factors,
    )
    return envelope.to_dict()


def build_regime_risk_envelope(
    *,
    regime_detection: list[dict[str, Any]] | None = None,
    regime_strategy_alignment: list[dict[str, Any]] | None = None,
    strategy_performance_memory: dict[str, Any] | None = None,
    strategy_weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    regime_aware_strategy_selector: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build risk envelope rows for all monitored epics."""
    if _OVERRIDE is not None:
        return list(_OVERRIDE)

    regime_by_epic = _index_by_epic(regime_detection)
    alignment_by_epic = _index_by_epic(regime_strategy_alignment)
    selector_by_epic = _index_by_epic(regime_aware_strategy_selector)
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
        decide_epic_risk_envelope(
            epic,
            regime_row=regime_by_epic.get(epic),
            alignment_row=alignment_by_epic.get(epic),
            performance_memory=strategy_performance_memory,
            weighting_advice=strategy_weighting_advice,
            adaptive_thresholds=adaptive_thresholds,
            session_review=session_review,
            selector_row=selector_by_epic.get(epic),
            hard_row=hard_by_epic.get(epic),
        )
        for epic in epics
    ]
