"""
Regime-aware sizing engine — Phase 8 advisory sizing recommendations (v38).

Produces adaptive, regime-aware sizing advice per epic. Advisory-only.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.adaptive_thresholds import BASELINE_THRESHOLDS
from runtime.regime_detection import MarketRegime

_OVERRIDE: list[dict[str, Any]] | None = None

_BASE_SIZE: dict[str, float] = {
    "SCALP": 0.10,
    "MOMENTUM": 0.25,
    "SWING": 0.40,
    "ROTATION": 0.30,
    "STAND_DOWN": 0.00,
}

_PROFILE_FLAGS: dict[str, str] = {
    "SCALP": "SCALP_SIZING",
    "MOMENTUM": "MOMENTUM_SIZING",
    "SWING": "SWING_SIZING",
    "ROTATION": "ROTATION_SIZING",
    "STAND_DOWN": "STAND_DOWN_ZERO_SIZE",
}

_RISK_SIZE_MODIFIER: dict[str, float] = {
    "ZERO": 0.0,
    "TIGHT": 0.75,
    "MEDIUM": 1.0,
    "WIDE": 1.15,
    "STRUCTURAL": 1.10,
}

_HIGH_SESSION_RISK = 60
_HIGH_SELECTOR_CONF = 75
_STRONG_PERFORMANCE = 58
_HIGH_QUALITY = 75


@dataclass
class RegimeSizingAdvice:
    epic: str
    recommended_size_factor: float
    sizing_confidence: int
    sizing_reason: str
    sizing_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "recommended_size_factor": round(float(self.recommended_size_factor), 4),
            "sizing_confidence": int(self.sizing_confidence),
            "sizing_reason": self.sizing_reason,
            "sizing_flags": sorted(set(self.sizing_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_regime_sizing_for_tests() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def set_regime_sizing_for_tests(advice: list[dict[str, Any]] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = advice


def _index_by_epic(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(r["epic"]): r for r in (rows or []) if r.get("epic")}


def _clamp_factor(value: float) -> float:
    return max(0.0, min(1.0, round(value, 4)))


def _win_rate_for_profile(performance_memory: dict[str, Any] | None, profile: str) -> float:
    win_rates = (performance_memory or {}).get("win_rates") or {}
    key = f"{profile.lower()}_win_rate"
    try:
        return float(win_rates.get(key) or 50.0)
    except (TypeError, ValueError):
        return 50.0


def _resolve_strategy(
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
    if profile in _BASE_SIZE:
        return profile
    profile = str((alignment_row or {}).get("recommended_profile") or "").upper()
    if profile in _BASE_SIZE:
        return profile
    return "MOMENTUM"


def _compute_sizing_confidence(
    *,
    regime_confidence: int,
    selector_confidence: int,
    risk_confidence: int,
    performance_confidence: int,
    session_quality: int,
    session_stability: int,
    threshold_delta: int = 0,
) -> int:
    session_component = (session_quality * 0.5 + session_stability * 0.5)
    raw = (
        regime_confidence * 0.30
        + selector_confidence * 0.25
        + risk_confidence * 0.20
        + performance_confidence * 0.15
        + session_component * 0.10
        + threshold_delta
    )
    return max(0, min(100, int(round(raw))))


def _threshold_confidence_delta(adaptive_thresholds: dict[str, Any] | None) -> int:
    adj = (adaptive_thresholds or {}).get("threshold_adjustments") or {}
    if not adj:
        return 0
    delta = 0
    soft = float(adj.get("SOFT_BLOCK_THRESHOLD") or BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"])
    if soft < BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"]:
        delta += 3
    if soft > BASELINE_THRESHOLDS["SOFT_BLOCK_THRESHOLD"]:
        delta -= 3
    return delta


def decide_epic_sizing_advice(
    epic: str,
    *,
    regime_row: dict[str, Any] | None = None,
    alignment_row: dict[str, Any] | None = None,
    risk_row: dict[str, Any] | None = None,
    performance_memory: dict[str, Any] | None = None,
    weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    selector_row: dict[str, Any] | None = None,
    hard_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive regime-aware sizing recommendation for one epic."""
    flags: list[str] = []
    reasons: list[str] = []

    strategy = _resolve_strategy(
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
        selector_conf = int((selector_row or {}).get("selector_confidence") or 50)
    except (TypeError, ValueError):
        selector_conf = 50
    try:
        risk_conf = int((risk_row or {}).get("risk_confidence") or 50)
    except (TypeError, ValueError):
        risk_conf = 50
    risk_profile = str((risk_row or {}).get("risk_profile") or "MEDIUM").upper()

    try:
        perf_conf = int((weighting_advice or {}).get("bias_confidence") or 0)
    except (TypeError, ValueError):
        perf_conf = 50

    quality = int((session_review or {}).get("session_quality_score") or 50)
    risk = int((session_review or {}).get("session_risk_score") or 30)
    stability = int((session_review or {}).get("session_stability_score") or 50)
    session_flags = set((session_review or {}).get("session_flags") or [])

    win_rate = _win_rate_for_profile(performance_memory, strategy)
    performance_conf = perf_conf if perf_conf else int(win_rate)

    if strategy == "STAND_DOWN":
        advice = RegimeSizingAdvice(
            epic=epic,
            recommended_size_factor=0.0,
            sizing_confidence=_compute_sizing_confidence(
                regime_confidence=regime_conf,
                selector_confidence=selector_conf,
                risk_confidence=risk_conf,
                performance_confidence=performance_conf,
                session_quality=quality,
                session_stability=stability,
            ),
            sizing_reason="STAND_DOWN — zero advisory size factor",
            sizing_flags=["STAND_DOWN_ZERO_SIZE"],
            contributing_factors={
                "regime": regime_class,
                "strategy": strategy,
                "risk_envelope": risk_profile,
                "performance_win_rate": win_rate,
                "thresholds": (adaptive_thresholds or {}).get("adjustment_flags") or [],
                "session_state": {"quality": quality, "risk": risk, "stability": stability},
                "enforcement_state": "active" if (hard_row or {}).get("active") else "idle",
            },
        )
        return advice.to_dict()

    factor = _BASE_SIZE.get(strategy, 0.25)
    flags.append(_PROFILE_FLAGS.get(strategy, "MOMENTUM_SIZING"))
    reasons.append(f"{strategy} base size factor {factor:.2f}")

    # Risk envelope modifier
    risk_mod = _RISK_SIZE_MODIFIER.get(risk_profile, 1.0)
    factor *= risk_mod
    if risk_mod != 1.0:
        flags.append("RISK_ENVELOPE_SIZING")
        reasons.append(f"risk envelope {risk_profile} modifier ×{risk_mod:.2f}")

    # Profile-specific regime and performance adjustments
    if strategy == "SCALP":
        if regime_class == MarketRegime.LOW_VOL.value and win_rate >= _STRONG_PERFORMANCE:
            factor *= 1.20
            flags.append("SCALP_SIZE_UP_LOW_VOL")
            reasons.append("low vol + strong performance — increase SCALP size")
        if selector_conf >= _HIGH_SELECTOR_CONF:
            factor *= 1.10
            flags.append("SCALP_SIZE_UP_SELECTOR")
        if regime_class in (MarketRegime.EXTREME_VOL.value, MarketRegime.BREAKOUT.value):
            factor *= 0.70
            flags.append("SCALP_SIZE_DOWN_EXTREME_VOL")
            reasons.append("extreme volatility — decrease SCALP size")
        if "FEED_DEGRADED" in session_flags or regime_class == MarketRegime.LIQUIDITY_DROP.value:
            factor *= 0.75
            flags.append("SCALP_SIZE_DOWN_FEED")
        if risk >= _HIGH_SESSION_RISK:
            factor *= 0.80
            flags.append("SCALP_SIZE_DOWN_SESSION_RISK")

    elif strategy == "MOMENTUM":
        if regime_class == MarketRegime.TREND.value and win_rate >= _STRONG_PERFORMANCE:
            factor *= 1.25
            flags.append("MOMENTUM_SIZE_UP_TREND")
            reasons.append("strong trend + momentum performance — increase size")
        if regime_class == MarketRegime.CHOP.value:
            factor *= 0.75
            flags.append("MOMENTUM_SIZE_DOWN_CHOP")
            reasons.append("chop regime — decrease MOMENTUM size")
        if regime_class in (MarketRegime.EXTREME_VOL.value, MarketRegime.BREAKOUT.value):
            factor *= 0.70
            flags.append("MOMENTUM_SIZE_DOWN_EXTREME_VOL")
        if _moderate_vol_regime(regime_class):
            factor *= 1.10
            flags.append("MOMENTUM_SIZE_UP_MEDIUM_VOL")

    elif strategy == "SWING":
        if regime_class == MarketRegime.LOW_VOL.value and win_rate >= _STRONG_PERFORMANCE:
            factor *= 1.20
            flags.append("SWING_SIZE_UP_LOW_VOL")
            reasons.append("low vol + swing performance — increase size")
        if regime_class in (MarketRegime.EXTREME_VOL.value, MarketRegime.LIQUIDITY_DROP.value):
            factor *= 0.65
            flags.append("SWING_SIZE_DOWN_EXTREME_LIQUIDITY")

    elif strategy == "ROTATION":
        if regime_class == MarketRegime.REVERSAL.value and win_rate >= 50:
            factor *= 1.20
            flags.append("ROTATION_SIZE_UP_REVERSAL")
            reasons.append("reversal regime — increase ROTATION size")
        if regime_class in (MarketRegime.BREAKOUT.value, MarketRegime.EXTREME_VOL.value):
            factor *= 0.70
            flags.append("ROTATION_SIZE_DOWN_BREAKOUT")

    if quality >= _HIGH_QUALITY and risk < 45:
        factor *= 1.05
        flags.append("SESSION_QUALITY_SIZE_UP")

    if risk >= _HIGH_SESSION_RISK:
        factor *= 0.85
        flags.append("SESSION_RISK_SIZE_DOWN")
        reasons.append("high session risk — reduce size")

    thresh_delta = _threshold_confidence_delta(adaptive_thresholds)
    if thresh_delta != 0:
        flags.append("THRESHOLD_CONFIDENCE_ADJUST")

    if hard_row and hard_row.get("active"):
        flags.append("HARD_ENFORCEMENT_ACTIVE")

    factor = _clamp_factor(factor)
    confidence = _compute_sizing_confidence(
        regime_confidence=regime_conf,
        selector_confidence=selector_conf,
        risk_confidence=risk_conf,
        performance_confidence=performance_conf,
        session_quality=quality,
        session_stability=stability,
        threshold_delta=thresh_delta,
    )

    advice = RegimeSizingAdvice(
        epic=epic,
        recommended_size_factor=factor,
        sizing_confidence=confidence,
        sizing_reason="; ".join(reasons[:4]),
        sizing_flags=flags,
        contributing_factors={
            "regime": regime_class,
            "strategy": strategy,
            "risk_envelope": risk_profile,
            "performance_win_rate": round(win_rate, 2),
            "thresholds": (adaptive_thresholds or {}).get("adjustment_flags") or [],
            "session_state": {"quality": quality, "risk": risk, "stability": stability},
            "enforcement_state": "active" if (hard_row or {}).get("active") else "idle",
        },
    )
    return advice.to_dict()


def _moderate_vol_regime(regime_class: str) -> bool:
    return regime_class in (MarketRegime.TREND.value, MarketRegime.CHOP.value, MarketRegime.UNKNOWN.value)


def build_regime_sizing_advice(
    *,
    regime_detection: list[dict[str, Any]] | None = None,
    regime_strategy_alignment: list[dict[str, Any]] | None = None,
    regime_risk_envelope: list[dict[str, Any]] | None = None,
    strategy_performance_memory: dict[str, Any] | None = None,
    strategy_weighting_advice: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    regime_aware_strategy_selector: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build sizing advice rows for all monitored epics."""
    if _OVERRIDE is not None:
        return list(_OVERRIDE)

    regime_by_epic = _index_by_epic(regime_detection)
    alignment_by_epic = _index_by_epic(regime_strategy_alignment)
    risk_by_epic = _index_by_epic(regime_risk_envelope)
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
        decide_epic_sizing_advice(
            epic,
            regime_row=regime_by_epic.get(epic),
            alignment_row=alignment_by_epic.get(epic),
            risk_row=risk_by_epic.get(epic),
            performance_memory=strategy_performance_memory,
            weighting_advice=strategy_weighting_advice,
            adaptive_thresholds=adaptive_thresholds,
            session_review=session_review,
            selector_row=selector_by_epic.get(epic),
            hard_row=hard_by_epic.get(epic),
        )
        for epic in epics
    ]
