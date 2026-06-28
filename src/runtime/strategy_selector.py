"""
Advisory-only strategy selector — recommendations for GUI/operator review.

Does NOT influence execution, sizing, dispatch, or Path A/B/micro plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from runtime.strategy_profile import SWING_HOLD_MIN_SEC, epic_z_pierce_active


class RecommendedStrategyProfile(str, Enum):
    SCALP = "SCALP"
    MOMENTUM = "MOMENTUM"
    SWING = "SWING"
    ROTATION = "ROTATION"
    STAND_DOWN = "STAND_DOWN"


FEED1_LATENCY_LOW_MS = 5_000.0
FEED1_STALE_MAX_SEC = 120.0
VOLATILITY_HIGH_Z = 1.75
VOLATILITY_EXTREME_Z = 2.5
VOLATILITY_MODERATE_Z = 1.0
SESSION_SCORE_LOW = 50
CRITICAL_ANOMALIES = frozenset(
    {
        "ALL_FEEDS_DEGRADED",
        "PRIMARY_FEED_STALE",
        "ORDER_PENDING_TOO_LONG",
        "NO_RECONCILE_AFTER_CLOSE",
        "MULTIPLE_EPICS_STALLED_IN_ORDER_PENDING",
    }
)


@dataclass
class StrategyAdvice:
    epic: str
    recommended_strategy_profile: RecommendedStrategyProfile
    confidence: int
    reason: str
    expected_horizon: str
    expected_risk_envelope: str
    expected_points_target: int
    advisory_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "recommended_strategy_profile": self.recommended_strategy_profile.value,
            "confidence": int(self.confidence),
            "reason": self.reason,
            "expected_horizon": self.expected_horizon,
            "expected_risk_envelope": self.expected_risk_envelope,
            "expected_points_target": int(self.expected_points_target),
            "advisory_flags": list(self.advisory_flags),
        }


def _parse_ts_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        normalized = str(ts).replace("Z", "+00:00")
        if "T" not in normalized and " " in normalized:
            normalized = normalized.replace(" ", "T")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (TypeError, ValueError):
        return None


def _feed1_meta(api_feed_health: dict[str, Any]) -> dict[str, Any]:
    feeds = api_feed_health.get("feeds") or {}
    return dict(feeds.get("feed1") or {})


def _feed1_low_latency(api_feed_health: dict[str, Any]) -> bool:
    meta = _feed1_meta(api_feed_health)
    latency = meta.get("latency_ms")
    if latency is None:
        return meta.get("status") == "OK"
    return float(latency) <= FEED1_LATENCY_LOW_MS


def _feed1_fresh(api_feed_health: dict[str, Any]) -> bool:
    meta = _feed1_meta(api_feed_health)
    if meta.get("status") != "OK":
        return False
    age = _parse_ts_age_sec(meta.get("last_update_timestamp"))
    if age is None:
        return True
    return age <= FEED1_STALE_MAX_SEC


def _feed_degraded(api_feed_health: dict[str, Any]) -> bool:
    feeds = api_feed_health.get("feeds") or {}
    if not feeds:
        return True
    return all((meta or {}).get("status") == "DEGRADED" for meta in feeds.values())


def _governance_for_epic(
    epic: str,
    pipeline_governance: dict[str, Any],
) -> dict[str, Any]:
    for row in pipeline_governance.get("per_epic") or []:
        if row.get("epic") == epic:
            return row
    return {}


def _volatility_z(epic: str) -> float | None:
    try:
        from runtime.dual_core_execution import get_stacked_snapshots

        snap = get_stacked_snapshots().get(epic)
        if snap is None:
            return None
        return float(snap.volatility_z_score)
    except Exception:
        return None


def _hub_spread_age(epic: str) -> tuple[float | None, float | None]:
    """Return (spread, quote_age_s) from hub — observability only."""
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is None or snap.bid <= 0:
            return None, None
        spread = float(snap.offer) - float(snap.bid)
        return spread, float(snap.age_seconds())
    except Exception:
        return None, None


def _recent_pnl_points(epic_row: dict[str, Any]) -> float | None:
    pnl = epic_row.get("unrealised_pnl")
    if pnl is not None:
        try:
            return float(pnl)
        except (TypeError, ValueError):
            pass
    return None


def _points_target_for(profile: RecommendedStrategyProfile, z: float | None) -> int:
    if profile is RecommendedStrategyProfile.SCALP:
        return 3 if z is not None and abs(z) >= VOLATILITY_HIGH_Z else 5
    if profile is RecommendedStrategyProfile.MOMENTUM:
        return 12
    if profile is RecommendedStrategyProfile.SWING:
        return 25
    if profile is RecommendedStrategyProfile.ROTATION:
        return 8
    return 0


def _score_stand_down(
    epic_row: dict[str, Any],
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
    session_governance: dict[str, Any],
    rotation_status: dict[str, Any],
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    reasons: list[str] = []

    if _feed_degraded(api_feed_health):
        score += 40
        flags.append("FEED_DEGRADED")
        reasons.append("all feeds degraded")

    session_score = int(session_governance.get("overall_session_health_score") or 100)
    if session_score < SESSION_SCORE_LOW:
        score += 25
        flags.append("SESSION_HEALTH_LOW")
        reasons.append(f"session score {session_score}")

    anomalies = set(gov_row.get("pipeline_anomalies") or [])
    anomalies.update(gov_row.get("feed_anomalies") or [])
    if anomalies & CRITICAL_ANOMALIES:
        score += 30
        flags.append("PIPELINE_CRITICAL")
        reasons.append("critical pipeline anomalies")

    z = _volatility_z(epic)
    if z is not None and abs(z) >= VOLATILITY_EXTREME_Z:
        score += 25
        flags.append("VOLATILITY_EXTREME")
        reasons.append(f"extreme volatility z={z:.2f}")

    active = set(rotation_status.get("active_markets") or [])
    candidates = set(rotation_status.get("candidate_markets") or [])
    if epic in candidates and epic not in active:
        score += 10
        flags.append("EPIC_COOLING")
        reasons.append("epic cooling in rotation universe")

    if gov_row.get("pipeline_health_score", 100) < 40:
        score += 15
        flags.append("EPIC_HEALTH_LOW")

    reason = "; ".join(reasons) if reasons else "elevated operational risk"
    return score, reason, flags


def _score_scalp(
    epic_row: dict[str, Any],
    gov_row: dict[str, Any],
    api_feed_health: dict[str, Any],
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    z = _volatility_z(epic)

    if _feed1_low_latency(api_feed_health) and _feed1_fresh(api_feed_health):
        score += 30
        flags.append("FEED_STRONG")
    elif _feed1_fresh(api_feed_health):
        score += 15

    if z is not None and abs(z) >= VOLATILITY_HIGH_Z:
        score += 25
        flags.append("VOLATILITY_HIGH")
    if epic_z_pierce_active(epic):
        score += 15
        flags.append("MEAN_REVERSION_CHANNEL")

    active = str(epic_row.get("active_strategy_profile") or "")
    source = str(epic_row.get("strategy_source") or "")
    state = str(epic_row.get("pipeline_state") or "")
    if active == "SCALP" or source == "MICRO":
        score += 20
        flags.append("MICRO_ACTIVE")
    if state in ("LIVE", "IN_PROFIT", "RECONCILED", "CLOSED") and source == "MICRO":
        score += 10
        flags.append("FAST_CYCLE")

    micro_anomalies = {
        a
        for a in (gov_row.get("pipeline_anomalies") or [])
        if a in ("ORDER_PENDING_TOO_LONG", "LIVE_WITHOUT_TRAILING_GUARDS")
    }
    if not micro_anomalies:
        score += 15
        flags.append("PIPELINE_STABLE")

    reason = "low-latency feed with volatility/micro-friendly conditions"
    return score, reason, flags


def _score_momentum(epic_row: dict[str, Any], gov_row: dict[str, Any], api_feed_health: dict[str, Any]) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    ml = epic_row.get("ml_appetite") or {}
    appetite = str(ml.get("appetite") or "").upper()

    if appetite in ("WEAK", "STRONG"):
        score += 30
        flags.append("ML_APPETITE_PRESENT")
    if epic_row.get("signal_ingested") and epic_row.get("order_prepared"):
        score += 25
        flags.append("PATH_A_LIFECYCLE")
    trailing = epic_row.get("trailing_guards") or {}
    if trailing.get("active"):
        score += 15
        flags.append("TRAILING_ACTIVE")
    if _feed1_fresh(api_feed_health):
        score += 15
        flags.append("FEED_STABLE")

    state = str(epic_row.get("pipeline_state") or "")
    if state in ("LIVE", "IN_PROFIT", "ORDER_PENDING", "SIGNAL_ONLY"):
        score += 10
        flags.append("PIPELINE_PROGRESSION")

    if not gov_row.get("pipeline_anomalies"):
        score += 10
        flags.append("GOVERNANCE_CLEAN")

    reason = "Path A lifecycle and ML appetite with stable feed health"
    return score, reason, flags


def _score_swing(epic_row: dict[str, Any], gov_row: dict[str, Any], epic: str) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    z = _volatility_z(epic)

    hold_age = _parse_ts_age_sec(
        epic_row.get("live_tracking_timestamp") or epic_row.get("order_confirmed_timestamp")
    )
    if epic_row.get("live_tracking") and hold_age is not None and hold_age >= SWING_HOLD_MIN_SEC:
        score += 40
        flags.append("LONG_DURATION_HOLD")

    ml = epic_row.get("ml_appetite") or {}
    if str(ml.get("appetite") or "").upper() in ("WEAK", "STRONG"):
        score += 15
        flags.append("ML_STABLE")

    if z is not None and VOLATILITY_MODERATE_Z <= abs(z) < VOLATILITY_HIGH_Z:
        score += 20
        flags.append("VOLATILITY_MODERATE")

    if "LIVE_WITHOUT_TRAILING_GUARDS" not in (gov_row.get("pipeline_anomalies") or []):
        score += 15
        flags.append("TRAILING_OK")

    if str(epic_row.get("active_strategy_profile") or "") == "SWING":
        score += 10

    reason = "extended Path A hold with moderate volatility"
    return score, reason, flags


def _score_rotation(
    epic_row: dict[str, Any],
    rotation_status: dict[str, Any],
    api_feed_health: dict[str, Any],
    epic: str,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    score = 0
    active = set(rotation_status.get("active_markets") or [])

    if epic in active:
        score += 35
        flags.append("ACTIVE_STACK")
    rot_state = str(rotation_status.get("rotation_state") or "IDLE").upper()
    if rot_state in ("EVALUATING", "ROTATING"):
        score += 25
        flags.append("ROTATION_ACTIVE")
    if epic_z_pierce_active(epic):
        score += 25
        flags.append("Z_PIERCE")

    feeds = api_feed_health.get("feeds") or {}
    ok_count = sum(1 for meta in feeds.values() if (meta or {}).get("status") == "OK")
    if ok_count >= 1:
        score += 10
        flags.append("FEED_MIXED_OK")

    reason = "rotation stack activity with pierce or evaluating state"
    return score, reason, flags


def _horizon_for(profile: RecommendedStrategyProfile) -> str:
    return {
        RecommendedStrategyProfile.SCALP: "minutes",
        RecommendedStrategyProfile.MOMENTUM: "minutes",
        RecommendedStrategyProfile.SWING: "hours",
        RecommendedStrategyProfile.ROTATION: "seconds",
        RecommendedStrategyProfile.STAND_DOWN: "minutes",
    }[profile]


def _risk_envelope_for(profile: RecommendedStrategyProfile, z: float | None) -> str:
    if profile is RecommendedStrategyProfile.STAND_DOWN:
        return "high"
    if profile is RecommendedStrategyProfile.SCALP:
        return "medium" if z is not None and abs(z) >= VOLATILITY_HIGH_Z else "low"
    if profile is RecommendedStrategyProfile.SWING:
        return "medium"
    if profile is RecommendedStrategyProfile.ROTATION:
        return "medium"
    return "low"


def advise_epic(
    epic_row: dict[str, Any],
    *,
    pipeline_governance: dict[str, Any],
    api_feed_health: dict[str, Any],
    market_rotation_status: dict[str, Any],
    session_governance: dict[str, Any],
) -> StrategyAdvice:
    """Produce advisory recommendation for a single epic — no side effects."""
    epic = str(epic_row.get("epic") or "")
    gov_row = _governance_for_epic(epic, pipeline_governance)
    z = _volatility_z(epic)

    candidates: list[tuple[RecommendedStrategyProfile, int, str, list[str]]] = []

    sd_score, sd_reason, sd_flags = _score_stand_down(
        epic_row, gov_row, api_feed_health, session_governance, market_rotation_status, epic
    )
    candidates.append((RecommendedStrategyProfile.STAND_DOWN, sd_score, sd_reason, sd_flags))

    sc_score, sc_reason, sc_flags = _score_scalp(epic_row, gov_row, api_feed_health, epic)
    candidates.append((RecommendedStrategyProfile.SCALP, sc_score, sc_reason, sc_flags))

    mo_score, mo_reason, mo_flags = _score_momentum(epic_row, gov_row, api_feed_health)
    candidates.append((RecommendedStrategyProfile.MOMENTUM, mo_score, mo_reason, mo_flags))

    sw_score, sw_reason, sw_flags = _score_swing(epic_row, gov_row, epic)
    candidates.append((RecommendedStrategyProfile.SWING, sw_score, sw_reason, sw_flags))

    ro_score, ro_reason, ro_flags = _score_rotation(epic_row, market_rotation_status, api_feed_health, epic)
    candidates.append((RecommendedStrategyProfile.ROTATION, ro_score, ro_reason, ro_flags))

    # STAND_DOWN wins when score exceeds threshold and beats others
    best = max(candidates, key=lambda item: item[1])
    profile, raw_score, reason, flags = best
    if (
        profile is not RecommendedStrategyProfile.STAND_DOWN
        and sd_score >= 55
        and sd_score >= raw_score
    ):
        profile, raw_score, reason, flags = (
            RecommendedStrategyProfile.STAND_DOWN,
            sd_score,
            sd_reason,
            sd_flags,
        )

    confidence = max(0, min(100, raw_score))
    if confidence < 20 and profile is not RecommendedStrategyProfile.STAND_DOWN:
        profile = RecommendedStrategyProfile.STAND_DOWN
        reason = "insufficient confidence for active strategy recommendation"
        flags = list(set(flags + ["LOW_CONFIDENCE"]))
        confidence = max(confidence, 25)

    spread, quote_age = _hub_spread_age(epic)
    if spread is not None and quote_age is not None and quote_age > FEED1_STALE_MAX_SEC:
        if "FEED_STALE" not in flags:
            flags.append("FEED_STALE")

    pnl = _recent_pnl_points(epic_row)
    if pnl is not None and pnl > 0 and "RECENT_PROFIT" not in flags:
        flags.append("RECENT_PROFIT")
    elif pnl is not None and pnl < 0:
        flags.append("RECENT_LOSS")

    return StrategyAdvice(
        epic=epic,
        recommended_strategy_profile=profile,
        confidence=confidence,
        reason=reason,
        expected_horizon=_horizon_for(profile),
        expected_risk_envelope=_risk_envelope_for(profile, z),
        expected_points_target=_points_target_for(profile, z),
        advisory_flags=sorted(set(flags)),
    )


def build_strategy_selector_advice(
    *,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    pipeline_governance: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    market_rotation_status: dict[str, Any] | None = None,
    session_governance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Build advisory recommendations for all epics in pipeline health.

    When inputs omitted, reads live observability snapshots (read-only).
    """
    if trade_pipeline_health is None or pipeline_governance is None or api_feed_health is None:
        from runtime.pipeline_governance import build_pipeline_governance
        from runtime.pipeline_health import (
            build_api_feed_health,
            build_market_rotation_status,
            build_trade_pipeline_health,
        )

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

    if market_rotation_status is None:
        from runtime.pipeline_health import build_market_rotation_status

        market_rotation_status = build_market_rotation_status()
    if session_governance is None:
        session_governance = {}

    advice: list[dict[str, Any]] = []
    for epic_row in trade_pipeline_health:
        epic = str(epic_row.get("epic") or "")
        if not epic:
            continue
        advice.append(
            advise_epic(
                epic_row,
                pipeline_governance=pipeline_governance,
                api_feed_health=api_feed_health,
                market_rotation_status=market_rotation_status,
                session_governance=session_governance,
            ).to_dict()
        )
    return advice
