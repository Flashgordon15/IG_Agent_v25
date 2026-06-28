"""
Regime detection engine — Phase 5 advisory market regime classification (v35).

Detects market regimes and strategy alignment from observability inputs only.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from runtime.strategy_selector import (
    VOLATILITY_EXTREME_Z,
    VOLATILITY_HIGH_Z,
    VOLATILITY_MODERATE_Z,
    _feed_degraded,
    _governance_for_epic,
    _hub_spread_age,
    _volatility_z,
)
from runtime.strategy_profile import epic_z_pierce_active

_OVERRIDE_DETECTION: list[dict[str, Any]] | None = None
_OVERRIDE_ALIGNMENT: list[dict[str, Any]] | None = None


class MarketRegime(str, Enum):
    TREND = "TREND"
    CHOP = "CHOP"
    BREAKOUT = "BREAKOUT"
    REVERSAL = "REVERSAL"
    EXTREME_VOL = "EXTREME_VOL"
    LOW_VOL = "LOW_VOL"
    LIQUIDITY_DROP = "LIQUIDITY_DROP"
    UNKNOWN = "UNKNOWN"


_EXTREME_Z = VOLATILITY_EXTREME_Z
_HIGH_Z = VOLATILITY_HIGH_Z
_MODERATE_Z = VOLATILITY_MODERATE_Z
_LOW_Z = 0.5


@dataclass
class RegimeDetection:
    epic: str
    regime_classification: str
    regime_confidence: int
    regime_reason: str
    regime_flags: list[str] = field(default_factory=list)
    time_of_day_bucket: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "regime_classification": self.regime_classification,
            "regime_confidence": int(self.regime_confidence),
            "regime_reason": self.regime_reason,
            "regime_flags": sorted(set(self.regime_flags)),
            "time_of_day_bucket": self.time_of_day_bucket,
        }


@dataclass
class RegimeStrategyAlignment:
    epic: str
    recommended_profile: str
    alignment_confidence: int
    alignment_reason: str
    alignment_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "recommended_profile": self.recommended_profile,
            "alignment_confidence": int(self.alignment_confidence),
            "alignment_reason": self.alignment_reason,
            "alignment_flags": sorted(set(self.alignment_flags)),
        }


def reset_regime_detection_for_tests() -> None:
    global _OVERRIDE_DETECTION, _OVERRIDE_ALIGNMENT
    _OVERRIDE_DETECTION = None
    _OVERRIDE_ALIGNMENT = None


def set_regime_detection_for_tests(
    *,
    detection: list[dict[str, Any]] | None = None,
    alignment: list[dict[str, Any]] | None = None,
) -> None:
    global _OVERRIDE_DETECTION, _OVERRIDE_ALIGNMENT
    _OVERRIDE_DETECTION = detection
    _OVERRIDE_ALIGNMENT = alignment


def _time_of_day_bucket(now: datetime | None = None) -> str:
    hour = (now or datetime.now(timezone.utc)).hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 16:
        return "london"
    return "us"


def _stack_snapshot(epic: str) -> dict[str, Any]:
    try:
        from runtime.dual_core_execution import get_stacked_snapshots

        snap = get_stacked_snapshots().get(epic)
        if snap is None:
            return {}
        return snap.as_dict() if hasattr(snap, "as_dict") else {}
    except Exception:
        return {}


def _atr_proxy(epic: str, stack: dict[str, Any]) -> float | None:
    upper = stack.get("micro_channel_upper")
    lower = stack.get("micro_channel_lower")
    if upper is not None and lower is not None:
        try:
            return abs(float(upper) - float(lower))
        except (TypeError, ValueError):
            pass
    z = stack.get("volatility_z_score")
    if z is not None:
        try:
            return abs(float(z)) * 0.01
        except (TypeError, ValueError):
            pass
    return None


def _chop_score(*, z: float | None, atr: float | None, pipeline_state: str) -> float:
    score = 0.0
    az = abs(z or 0.0)
    if az < _LOW_Z:
        score += 35.0
    elif az < _MODERATE_Z:
        score += 25.0
    if atr is not None and atr < 0.005:
        score += 20.0
    if pipeline_state in ("IDLE", "SIGNAL_ONLY"):
        score += 15.0
    return min(100.0, score)


def _trend_score(*, z: float | None, stack: dict[str, Any], epic_row: dict[str, Any]) -> float:
    score = 0.0
    az = abs(z or 0.0)
    if _MODERATE_Z <= az <= _HIGH_Z:
        score += 40.0
    if stack.get("core_a_macro_active"):
        score += 25.0
    if str(epic_row.get("active_strategy_profile") or "").upper() in ("MOMENTUM", "SWING"):
        score += 15.0
    if epic_row.get("pipeline_state") in ("LIVE", "IN_PROFIT", "IN_LOSS"):
        score += 10.0
    chop = _chop_score(z=z, atr=_atr_proxy(str(epic_row.get("epic") or ""), stack), pipeline_state=str(epic_row.get("pipeline_state") or ""))
    score = max(0.0, score - chop * 0.2)
    return min(100.0, score)


def _liquidity_score(
    *,
    epic_row: dict[str, Any],
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
) -> float:
    score = 0.0
    state = str(epic_row.get("pipeline_state") or "IDLE")
    if state in ("IDLE", "SIGNAL_ONLY") and not epic_row.get("order_dispatched"):
        score += 30.0
    anomalies = gov_row.get("pipeline_anomalies") or []
    if anomalies:
        score += min(40.0, len(anomalies) * 15.0)
    if _feed_degraded(api_feed_health):
        score += 25.0
    if not epic_row.get("signal_ingested"):
        score += 10.0
    return min(100.0, score)


def _rotation_stack_active(
    epic: str,
    market_rotation_status: dict[str, Any] | None,
) -> bool:
    rotation = market_rotation_status or {}
    active = set(rotation.get("active_markets") or [])
    if epic in active:
        return True
    try:
        from runtime.dual_core_execution import get_active_stack_epics

        return epic in set(get_active_stack_epics())
    except Exception:
        return False


def detect_epic_regime(
    epic: str,
    *,
    epic_row: dict[str, Any] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify market regime for one epic — advisory only."""
    row = dict(epic_row or {})
    row.setdefault("epic", epic)
    gov_row = _governance_for_epic(epic, pipeline_governance or {})
    stack = _stack_snapshot(epic)
    z = row.get("volatility_z")
    if z is None:
        z = _volatility_z(epic)
    try:
        zf = float(z) if z is not None else 0.0
    except (TypeError, ValueError):
        zf = 0.0

    spread, quote_age = _hub_spread_age(epic)
    if row.get("spread") is not None:
        try:
            spread = float(row["spread"])
        except (TypeError, ValueError):
            pass

    atr = _atr_proxy(epic, stack)
    pipeline_state = str(row.get("pipeline_state") or "IDLE")
    pierce = bool(row.get("pierce_active")) if "pierce_active" in row else epic_z_pierce_active(epic)
    rotation_active = bool(row.get("rotation_active")) if "rotation_active" in row else _rotation_stack_active(
        epic, market_rotation_status
    )
    tod = _time_of_day_bucket(now)

    scores: dict[MarketRegime, float] = {r: 0.0 for r in MarketRegime if r != MarketRegime.UNKNOWN}
    flags: list[str] = []
    reasons: list[str] = []

    liq = _liquidity_score(epic_row=row, gov_row=gov_row, api_feed_health=api_feed_health or {})
    scores[MarketRegime.LIQUIDITY_DROP] = liq
    if liq >= 55:
        flags.append("LIQUIDITY_DROP_REGIME")
        reasons.append(f"liquidity score {liq:.0f} — pipeline/feed activity low")

    if abs(zf) >= _EXTREME_Z:
        scores[MarketRegime.EXTREME_VOL] = min(100.0, 60 + abs(zf) * 10)
        flags.append("EXTREME_VOL_REGIME")
        reasons.append(f"|Z|={abs(zf):.2f}≥{_EXTREME_Z} extreme volatility")
    elif abs(zf) < _LOW_Z:
        scores[MarketRegime.LOW_VOL] = min(100.0, 70 - abs(zf) * 20)
        flags.append("LOW_VOL_REGIME")
        reasons.append(f"|Z|={abs(zf):.2f}<{_LOW_Z} narrow range")

    if pierce and (rotation_active or stack.get("core_b_micro_active")):
        scores[MarketRegime.REVERSAL] = 75.0
        flags.append("REVERSAL_REGIME")
        reasons.append("pierce detection with rotation/micro stack active")

    spread_wide = spread is not None and spread > 0 and (quote_age is None or quote_age < 120)
    if abs(zf) >= _HIGH_Z and spread_wide:
        breakout = min(100.0, 50 + abs(zf) * 15)
        if spread and spread > 0.0005:
            breakout += 10
        scores[MarketRegime.BREAKOUT] = breakout
        flags.append("BREAKOUT_REGIME")
        reasons.append(f"volatility spike |Z|={abs(zf):.2f} with spread expansion")

    chop = _chop_score(z=zf, atr=atr, pipeline_state=pipeline_state)
    scores[MarketRegime.CHOP] = chop
    if chop >= 50:
        flags.append("CHOP_REGIME")
        reasons.append(f"chop score {chop:.0f} — mean-reversion conditions")

    trend = _trend_score(z=zf, stack=stack, epic_row=row)
    scores[MarketRegime.TREND] = trend
    if trend >= 50:
        flags.append("TREND_REGIME")
        reasons.append(f"trend score {trend:.0f} — directional persistence")

    primary = max(scores.items(), key=lambda kv: kv[1])
    if primary[1] < 35:
        classification = MarketRegime.UNKNOWN.value
        confidence = 35
        reason = "insufficient regime signal — mixed/neutral conditions"
    else:
        classification = primary[0].value
        confidence = min(95, max(40, int(primary[1])))
        reason = "; ".join(reasons[:3]) if reasons else f"{classification} regime dominant"

    detection = RegimeDetection(
        epic=epic,
        regime_classification=classification,
        regime_confidence=confidence,
        regime_reason=reason,
        regime_flags=flags,
        time_of_day_bucket=tod,
    )
    return detection.to_dict()


def align_strategy_to_regime(
    regime: dict[str, Any],
    *,
    strategy_weighting_advice: dict[str, Any] | None = None,
    selector_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map detected regime to recommended strategy profile — advisory only."""
    classification = str(regime.get("regime_classification") or MarketRegime.UNKNOWN.value).upper()
    flags: list[str] = []
    confidence = int(regime.get("regime_confidence") or 50)
    reason_parts: list[str] = []

    mapping: dict[str, tuple[str, str]] = {
        MarketRegime.TREND.value: ("MOMENTUM", "TREND→MOMENTUM alignment"),
        MarketRegime.CHOP.value: ("SCALP", "CHOP→SCALP alignment"),
        MarketRegime.BREAKOUT.value: ("MOMENTUM", "BREAKOUT→MOMENTUM alignment"),
        MarketRegime.REVERSAL.value: ("ROTATION", "REVERSAL→ROTATION alignment"),
        MarketRegime.EXTREME_VOL.value: ("STAND_DOWN", "EXTREME_VOL→STAND_DOWN default"),
        MarketRegime.LOW_VOL.value: ("SWING", "LOW_VOL→SWING alignment"),
        MarketRegime.LIQUIDITY_DROP.value: ("STAND_DOWN", "LIQUIDITY_DROP→STAND_DOWN alignment"),
    }

    profile, align_reason = mapping.get(classification, ("MOMENTUM", "neutral fallback"))
    reason_parts.append(align_reason)
    flags.append(f"{classification}_ALIGNMENT")

    if classification == MarketRegime.BREAKOUT.value:
        z_flags = regime.get("regime_flags") or []
        if "LOW_VOL_REGIME" not in z_flags:
            profile = "MOMENTUM"
        else:
            profile = "SWING"
            reason_parts.append("breakout from low-vol base → SWING horizon")
            flags.append("BREAKOUT_SWING_VARIANT")

    if classification == MarketRegime.EXTREME_VOL.value:
        weight_bias = (strategy_weighting_advice or {}).get("recommended_bias")
        if weight_bias == "SCALP" and int((strategy_weighting_advice or {}).get("bias_confidence") or 0) >= 60:
            profile = "SCALP"
            reason_parts.append("performance memory favours SCALP in extreme vol")
            flags.append("EXTREME_VOL_SCALP_OVERRIDE")

    if classification == MarketRegime.LOW_VOL.value:
        if (selector_row or {}).get("recommended_strategy_profile") == "SCALP":
            profile = "SCALP"
            reason_parts.append("selector recommends SCALP in low-vol chop")
            flags.append("LOW_VOL_SCALP_VARIANT")

    sel_profile = (selector_row or {}).get("recommended_strategy_profile")
    if sel_profile and str(sel_profile).upper() == profile:
        confidence = min(95, confidence + 10)
        flags.append("SELECTOR_AGREEMENT")
    elif sel_profile and str(sel_profile).upper() != profile:
        flags.append("SELECTOR_DIVERGENCE")

    alignment = RegimeStrategyAlignment(
        epic=str(regime.get("epic") or ""),
        recommended_profile=profile,
        alignment_confidence=confidence,
        alignment_reason="; ".join(reason_parts),
        alignment_flags=flags,
    )
    return alignment.to_dict()


def build_regime_detection(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if _OVERRIDE_DETECTION is not None:
        return list(_OVERRIDE_DETECTION)

    rows = trade_pipeline_health or []
    if not rows:
        return []

    return [
        detect_epic_regime(
            str(row.get("epic") or ""),
            epic_row=row,
            pipeline_governance=pipeline_governance,
            api_feed_health=api_feed_health,
            market_rotation_status=market_rotation_status,
            now=now,
        )
        for row in rows
        if row.get("epic")
    ]


def build_regime_strategy_alignment(
    *,
    regime_detection: list[dict[str, Any]] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_weighting_advice: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if _OVERRIDE_ALIGNMENT is not None:
        return list(_OVERRIDE_ALIGNMENT)

    selector_by_epic = {r["epic"]: r for r in (strategy_selector_advice or []) if r.get("epic")}
    return [
        align_strategy_to_regime(
            regime,
            strategy_weighting_advice=strategy_weighting_advice,
            selector_row=selector_by_epic.get(regime.get("epic") or ""),
        )
        for regime in (regime_detection or [])
    ]


def build_regime_detection_bundle(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_weighting_advice: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full Phase 5 bundle for GUI status."""
    detection = build_regime_detection(
        trade_pipeline_health=trade_pipeline_health,
        pipeline_governance=pipeline_governance,
        api_feed_health=api_feed_health,
        market_rotation_status=market_rotation_status,
        now=now,
    )
    alignment = build_regime_strategy_alignment(
        regime_detection=detection,
        strategy_selector_advice=strategy_selector_advice,
        strategy_weighting_advice=strategy_weighting_advice,
    )
    return {
        "regime_detection": detection,
        "regime_strategy_alignment": alignment,
    }
