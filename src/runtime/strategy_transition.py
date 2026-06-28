"""
Advisory-only strategy transition engine — profile change recommendations for GUI.

Does NOT influence execution, sizing, dispatch, or Path A/B/micro plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.strategy_profile import SWING_HOLD_MIN_SEC, epic_z_pierce_active
from runtime.strategy_selector import (
    CRITICAL_ANOMALIES,
    SESSION_SCORE_LOW,
    VOLATILITY_EXTREME_Z,
    VOLATILITY_HIGH_Z,
    VOLATILITY_MODERATE_Z,
    _feed1_fresh,
    _feed1_low_latency,
    _feed_degraded,
    _governance_for_epic,
    _parse_ts_age_sec,
    _recent_pnl_points,
    _volatility_z,
)


class TransitionProfile(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    STAND_DOWN = "STAND_DOWN"
    UNKNOWN = "UNKNOWN"


SELECTOR_LEAN_THRESHOLD = 55
STAND_DOWN_SCORE_THRESHOLD = 50
STABLE_CONFIDENCE = 35


@dataclass
class StrategyTransitionAdvice:
    epic: str
    current_profile: str
    target_profile: str
    transition_confidence: int
    transition_reason: str
    transition_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "current_profile": self.current_profile,
            "target_profile": self.target_profile,
            "transition_confidence": int(self.transition_confidence),
            "transition_reason": self.transition_reason,
            "transition_flags": sorted(set(self.transition_flags)),
        }


def _normalize_profile(raw: str | None) -> TransitionProfile:
    value = str(raw or TransitionProfile.UNKNOWN.value).upper()
    try:
        return TransitionProfile(value)
    except ValueError:
        return TransitionProfile.UNKNOWN


def _resolve_current_profile(
    epic_row: dict[str, Any],
    selector_advice: dict[str, Any] | None,
) -> TransitionProfile:
    active = _normalize_profile(epic_row.get("active_strategy_profile"))
    if active is not TransitionProfile.UNKNOWN:
        return active
    if selector_advice:
        return _normalize_profile(selector_advice.get("recommended_strategy_profile"))
    return TransitionProfile.UNKNOWN


def _selector_leans_to(selector_advice: dict[str, Any] | None, profile: TransitionProfile) -> bool:
    if not selector_advice:
        return False
    rec = _normalize_profile(selector_advice.get("recommended_strategy_profile"))
    try:
        conf = int(selector_advice.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0
    return rec is profile and conf >= SELECTOR_LEAN_THRESHOLD


def _ml_strengthened(epic_row: dict[str, Any]) -> bool:
    ml = epic_row.get("ml_appetite") or {}
    appetite = str(ml.get("appetite") or "").upper()
    try:
        prob = float(ml.get("probability") or 0.0)
    except (TypeError, ValueError):
        prob = 0.0
    return appetite in ("WEAK", "STRONG") and prob >= 0.55


def _governance_clean(gov_row: dict[str, Any]) -> bool:
    if gov_row.get("pipeline_anomalies") or gov_row.get("feed_anomalies"):
        return False
    try:
        return int(gov_row.get("pipeline_health_score") or 100) >= 70
    except (TypeError, ValueError):
        return True


def _feed_strong(api_feed_health: dict[str, Any]) -> bool:
    return _feed1_low_latency(api_feed_health) and _feed1_fresh(api_feed_health)


def _micro_profitable(epic_row: dict[str, Any]) -> bool:
    source = str(epic_row.get("strategy_source") or "").upper()
    pnl = _recent_pnl_points(epic_row)
    if source == "MICRO" and pnl is not None and pnl > 0:
        return True
    if epic_row.get("order_confirmed") and source == "MICRO":
        pnl = _recent_pnl_points(epic_row)
        return pnl is None or pnl >= 0
    return False


def _score_stand_down_transition(
    epic_row: dict[str, Any],
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
    session_governance: dict[str, Any],
    selector_advice: dict[str, Any] | None,
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    reasons: list[str] = []

    if _feed_degraded(api_feed_health):
        score += 40
        flags.append("FEED_DEGRADED")
        reasons.append("feed health degraded")

    session_score = int(session_governance.get("overall_session_health_score") or 100)
    if session_score < SESSION_SCORE_LOW:
        score += 25
        flags.append("SESSION_HEALTH_LOW")
        reasons.append(f"session governance score {session_score}")

    anomalies = set(gov_row.get("pipeline_anomalies") or [])
    anomalies.update(gov_row.get("feed_anomalies") or [])
    if anomalies & CRITICAL_ANOMALIES:
        score += 35
        flags.append("PIPELINE_CRITICAL")
        reasons.append("critical pipeline anomalies")

    z = _volatility_z(epic)
    if z is not None and abs(z) >= VOLATILITY_EXTREME_Z:
        score += 25
        flags.append("VOLATILITY_SPIKE")
        reasons.append("erratic extreme volatility")

    if _selector_leans_to(selector_advice, TransitionProfile.STAND_DOWN):
        score += 30
        flags.append("SELECTOR_STAND_DOWN")
        reasons.append("selector advises stand down")

    reason = "; ".join(reasons) if reasons else "elevated operational risk"
    return score, reason, flags


def _score_scalp_to_momentum(
    epic_row: dict[str, Any],
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
    selector_advice: dict[str, Any] | None,
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    z = _volatility_z(epic)

    if z is not None and abs(z) < VOLATILITY_HIGH_Z:
        score += 25
        flags.append("VOLATILITY_DROP")
    if _feed_strong(api_feed_health):
        score += 20
        flags.append("FEED_STABLE")
    if _ml_strengthened(epic_row):
        score += 25
        flags.append("ML_STRENGTHENED")
    if _governance_clean(gov_row):
        score += 15
        flags.append("PIPELINE_STABLE")
    if _selector_leans_to(selector_advice, TransitionProfile.MOMENTUM):
        score += 20
        flags.append("SELECTOR_MOMENTUM")

    reason = "volatility easing with stronger ML and stable feed — consider MOMENTUM"
    return score, reason, flags


def _score_momentum_to_scalp(
    epic_row: dict[str, Any],
    selector_advice: dict[str, Any] | None,
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    z = _volatility_z(epic)

    if z is not None and abs(z) >= VOLATILITY_HIGH_Z:
        score += 30
        flags.append("VOLATILITY_SPIKE")
    if epic_z_pierce_active(epic):
        score += 20
        flags.append("MEAN_REVERSION_CHANNEL")
    if _micro_profitable(epic_row):
        score += 20
        flags.append("MICRO_PROFITABLE")
    if _selector_leans_to(selector_advice, TransitionProfile.SCALP):
        score += 25
        flags.append("SELECTOR_SCALP")

    reason = "volatility spike / mean-reversion — consider SCALP"
    return score, reason, flags


def _score_rotation_to_scalp(
    epic_row: dict[str, Any],
    rotation_status: dict[str, Any],
    api_feed_health: dict[str, Any],
    selector_advice: dict[str, Any] | None,
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    active = set(rotation_status.get("active_markets") or [])

    if epic in active:
        score += 30
        flags.append("ROTATION_STACK_ACTIVE")
    if epic_z_pierce_active(epic):
        score += 25
        flags.append("Z_PIERCE")
    if _feed_strong(api_feed_health):
        score += 20
        flags.append("FEED_STRONG")
    if selector_advice:
        rec = _normalize_profile(selector_advice.get("recommended_strategy_profile"))
        try:
            conf = int(selector_advice.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        if rec is TransitionProfile.SCALP and conf >= SELECTOR_LEAN_THRESHOLD:
            score += 25
            flags.append("SELECTOR_SCALP_HIGH")

    reason = "rotation stack pierce with strong feed — consider SCALP handoff"
    return score, reason, flags


def _score_momentum_to_swing(epic_row: dict[str, Any], epic: str) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    hold_age = _parse_ts_age_sec(
        epic_row.get("live_tracking_timestamp") or epic_row.get("order_confirmed_timestamp")
    )
    z = _volatility_z(epic)

    if epic_row.get("live_tracking") and hold_age is not None and hold_age >= SWING_HOLD_MIN_SEC:
        score += 35
        flags.append("LONG_HOLD")
    if z is not None and VOLATILITY_MODERATE_Z <= abs(z) < VOLATILITY_HIGH_Z:
        score += 20
        flags.append("VOLATILITY_MODERATE")

    reason = "extended hold with moderate volatility — consider SWING"
    return score, reason, flags


def advise_epic_transition(
    epic_row: dict[str, Any],
    *,
    pipeline_governance: dict[str, Any],
    api_feed_health: dict[str, Any],
    market_rotation_status: dict[str, Any],
    session_governance: dict[str, Any],
    selector_advice_row: dict[str, Any] | None = None,
) -> StrategyTransitionAdvice:
    """Produce advisory transition recommendation for one epic — no side effects."""
    epic = str(epic_row.get("epic") or "")
    gov_row = _governance_for_epic(epic, pipeline_governance)
    current = _resolve_current_profile(epic_row, selector_advice_row)

    candidates: list[tuple[TransitionProfile, int, str, list[str]]] = []

    sd_score, sd_reason, sd_flags = _score_stand_down_transition(
        epic_row,
        gov_row,
        api_feed_health,
        session_governance,
        selector_advice_row,
        epic,
    )
    candidates.append((TransitionProfile.STAND_DOWN, sd_score, sd_reason, sd_flags))

    if current in (TransitionProfile.SCALP, TransitionProfile.UNKNOWN):
        sm_score, sm_reason, sm_flags = _score_scalp_to_momentum(
            epic_row, gov_row, api_feed_health, selector_advice_row, epic
        )
        candidates.append((TransitionProfile.MOMENTUM, sm_score, sm_reason, sm_flags))

    if current in (TransitionProfile.MOMENTUM, TransitionProfile.UNKNOWN):
        ms_score, ms_reason, ms_flags = _score_momentum_to_scalp(
            epic_row, selector_advice_row, epic
        )
        candidates.append((TransitionProfile.SCALP, ms_score, ms_reason, ms_flags))
        sw_score, sw_reason, sw_flags = _score_momentum_to_swing(epic_row, epic)
        candidates.append((TransitionProfile.SWING, sw_score, sw_reason, sw_flags))

    if current in (TransitionProfile.ROTATION, TransitionProfile.UNKNOWN):
        rs_score, rs_reason, rs_flags = _score_rotation_to_scalp(
            epic_row, market_rotation_status, api_feed_health, selector_advice_row, epic
        )
        candidates.append((TransitionProfile.SCALP, rs_score, rs_reason, rs_flags))

    best_target, best_score, best_reason, best_flags = max(
        candidates,
        key=lambda item: item[1],
    )

    current_value = current.value if current is not TransitionProfile.UNKNOWN else "UNKNOWN"

    if best_target is TransitionProfile.STAND_DOWN and best_score >= STAND_DOWN_SCORE_THRESHOLD:
        target = TransitionProfile.STAND_DOWN
        confidence = min(100, best_score)
        reason = best_reason
        flags = best_flags
    elif best_target.value != current_value and best_score >= 40:
        target = best_target
        confidence = min(100, best_score)
        reason = best_reason
        flags = best_flags
    else:
        target = current if current is not TransitionProfile.UNKNOWN else TransitionProfile.UNKNOWN
        target_value = target.value
        confidence = STABLE_CONFIDENCE
        reason = f"no transition recommended — maintain {current_value or 'UNKNOWN'}"
        flags = ["PROFILE_STABLE"]
        if _governance_clean(gov_row):
            flags.append("PIPELINE_STABLE")

    return StrategyTransitionAdvice(
        epic=epic,
        current_profile=current_value,
        target_profile=target.value if target is not TransitionProfile.UNKNOWN else "UNKNOWN",
        transition_confidence=confidence,
        transition_reason=reason,
        transition_flags=flags,
    )


def build_strategy_transition_advice(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build advisory transition recommendations for all epics (read-only)."""
    if trade_pipeline_health is None or pipeline_governance is None or api_feed_health is None:
        from runtime.pipeline_governance import build_pipeline_governance
        from runtime.pipeline_health import (
            build_api_feed_health,
            build_market_rotation_status,
            build_trade_pipeline_health,
        )
        from runtime.strategy_selector import build_strategy_selector_advice

        if trade_pipeline_health is None:
            trade_pipeline_health = build_trade_pipeline_health()
        if api_feed_health is None:
            api_feed_health = build_api_feed_health()
        if market_rotation_status is None:
            market_rotation_status = build_market_rotation_status()
        if pipeline_governance is None:
            bundle = build_pipeline_governance(
                trade_pipeline_health=trade_pipeline_health,
                api_feed_health=api_feed_health,
                market_rotation_status=market_rotation_status,
            )
            pipeline_governance = bundle.get("pipeline_governance") or {}
            session_governance = bundle.get("session_governance") or {}
        if strategy_selector_advice is None:
            strategy_selector_advice = build_strategy_selector_advice(
                trade_pipeline_health=trade_pipeline_health,
                pipeline_governance=pipeline_governance,
                api_feed_health=api_feed_health,
                market_rotation_status=market_rotation_status,
                session_governance=session_governance,
            )

    if market_rotation_status is None:
        from runtime.pipeline_health import build_market_rotation_status

        market_rotation_status = build_market_rotation_status()
    if session_governance is None:
        session_governance = {}

    advice_by_epic: dict[str, dict[str, Any]] = {}
    for row in strategy_selector_advice or []:
        epic = str(row.get("epic") or "")
        if epic:
            advice_by_epic[epic] = row

    advice: list[dict[str, Any]] = []
    for epic_row in trade_pipeline_health:
        epic = str(epic_row.get("epic") or "")
        if not epic:
            continue
        advice.append(
            advise_epic_transition(
                epic_row,
                pipeline_governance=pipeline_governance,
                api_feed_health=api_feed_health,
                market_rotation_status=market_rotation_status,
                session_governance=session_governance,
                selector_advice_row=advice_by_epic.get(epic),
            ).to_dict()
        )
    return advice
