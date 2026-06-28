"""
Strategy performance memory — Phase 4 advisory historical tracking (v34).

Maintains rolling performance memory and strategy weighting suggestions.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_PROFILES = ("SCALP", "MOMENTUM", "SWING", "ROTATION")
_VOL_REGIMES = ("low", "medium", "high", "extreme")
_FEED_REGIMES = ("strong", "degraded", "mixed")
_TOD_BUCKETS = ("asia", "london", "us")

_EMA_ALPHA = 0.25
_DEFAULT_WIN_RATE = 50.0

_MEMORY: dict[str, Any] = {}
_OVERRIDE_SUMMARY: dict[str, Any] | None = None
_OVERRIDE_WEIGHTING: dict[str, Any] | None = None


def _empty_memory() -> dict[str, Any]:
    profile_rates = {f"{p.lower()}_win_rate": _DEFAULT_WIN_RATE for p in _PROFILES}
    regime_template = {p: _DEFAULT_WIN_RATE for p in _PROFILES}
    return {
        **profile_rates,
        "per_epic_strategy_performance": {},
        "per_volatility_regime_performance": {r: dict(regime_template) for r in _VOL_REGIMES},
        "per_feed_health_regime_performance": {r: dict(regime_template) for r in _FEED_REGIMES},
        "per_time_of_day_performance": {b: dict(regime_template) for b in _TOD_BUCKETS},
        "drawdown_recovery_stats": {
            p: {
                "avg_recovery_sessions": 0.0,
                "recovery_count": 0,
                "last_drawdown_pct": 0.0,
            }
            for p in _PROFILES
        },
        "missed_opportunity_stats": {
            "missed_opportunity_score": 0.0,
            "missed_opportunity_count": 0,
            "last_reason": None,
        },
        "observation_count": 0,
    }


def reset_strategy_performance_memory_for_tests() -> None:
    global _MEMORY, _OVERRIDE_SUMMARY, _OVERRIDE_WEIGHTING
    _MEMORY = {}
    _OVERRIDE_SUMMARY = None
    _OVERRIDE_WEIGHTING = None


def set_strategy_performance_memory_for_tests(
    *,
    summary: dict[str, Any] | None = None,
    weighting: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
) -> None:
    global _MEMORY, _OVERRIDE_SUMMARY, _OVERRIDE_WEIGHTING
    if memory is not None:
        _MEMORY = copy.deepcopy(memory)
    _OVERRIDE_SUMMARY = summary
    _OVERRIDE_WEIGHTING = weighting


def _get_memory() -> dict[str, Any]:
    global _MEMORY
    if not _MEMORY:
        _MEMORY = _empty_memory()
    return _MEMORY


def _ema(current: float, observation: float, alpha: float = _EMA_ALPHA) -> float:
    return round((1.0 - alpha) * current + alpha * observation, 2)


def _volatility_regime(vol_summary: dict[str, Any] | None) -> str:
    mean_z = (vol_summary or {}).get("mean_z")
    if mean_z is None:
        return "medium"
    try:
        z = float(mean_z)
    except (TypeError, ValueError):
        return "medium"
    if z < 0.5:
        return "low"
    if z < 1.5:
        return "medium"
    if z < 2.5:
        return "high"
    return "extreme"


def _feed_regime(feed_summary: dict[str, Any] | None, api_feed_health: dict[str, Any] | None) -> str:
    overall = str((feed_summary or {}).get("overall") or "").upper()
    if overall == "DEGRADED":
        return "degraded"
    feeds = (api_feed_health or {}).get("feeds") or {}
    if not feeds:
        return "strong" if overall in ("", "OK", "STRONG") else "mixed"
    statuses = {str(v.get("status") or "").upper() for v in feeds.values() if isinstance(v, dict)}
    if not statuses:
        return "strong"
    if statuses == {"OK"} or statuses == {"STRONG"}:
        return "strong"
    if statuses <= {"DEGRADED", "OFFLINE", "FAILED"}:
        return "degraded"
    return "mixed"


def _time_of_day_bucket(now: datetime | None = None) -> str:
    hour = (now or datetime.now(timezone.utc)).hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 16:
        return "london"
    return "us"


def _session_win_rate_pct(points: dict[str, Any] | None) -> float | None:
    points = points or {}
    wins = int(points.get("closed_wins") or 0)
    losses = int(points.get("closed_losses") or 0)
    total = wins + losses
    if total <= 0:
        return None
    return round(100.0 * wins / total, 2)


def _profile_weights(trades_by_profile: dict[str, Any] | None) -> dict[str, float]:
    trades_by_profile = trades_by_profile or {}
    weights = {p: float(trades_by_profile.get(p) or 0) for p in _PROFILES}
    total = sum(weights.values())
    if total <= 0:
        return {p: 0.25 for p in _PROFILES}
    return {p: weights[p] / total for p in _PROFILES}


def _dominant_profile(time_in_profile: dict[str, Any] | None) -> str:
    time_in = time_in_profile or {}
    best = "MOMENTUM"
    best_val = -1.0
    for p in _PROFILES:
        val = float(time_in.get(p) or 0)
        if val > best_val:
            best_val = val
            best = p
    return best


def _update_regime_rates(
    memory: dict[str, Any],
    regime_key: str,
    regime_bucket: str,
    profile: str,
    observation: float,
) -> None:
    bucket = memory[regime_key][regime_bucket]
    current = float(bucket.get(profile) or _DEFAULT_WIN_RATE)
    bucket[profile] = _ema(current, observation)


def _update_epic_performance(
    memory: dict[str, Any],
    epic: str,
    profile: str,
    observation: float,
) -> None:
    per_epic = memory["per_epic_strategy_performance"]
    row = per_epic.setdefault(
        epic,
        {p: {"wins": 0, "losses": 0, "win_rate": _DEFAULT_WIN_RATE, "observations": 0} for p in _PROFILES},
    )
    cell = row.setdefault(
        profile,
        {"wins": 0, "losses": 0, "win_rate": _DEFAULT_WIN_RATE, "observations": 0},
    )
    cell["observations"] = int(cell.get("observations") or 0) + 1
    if observation >= 50.0:
        cell["wins"] = int(cell.get("wins") or 0) + 1
    else:
        cell["losses"] = int(cell.get("losses") or 0) + 1
    cell["win_rate"] = _ema(float(cell.get("win_rate") or _DEFAULT_WIN_RATE), observation)


def _update_drawdown_recovery(
    memory: dict[str, Any],
    *,
    drawdown: dict[str, Any] | None,
    time_in_profile: dict[str, Any] | None,
    points: dict[str, Any] | None,
) -> None:
    drawdown = drawdown or {}
    try:
        max_dd = float(drawdown.get("max_drawdown_pct") or 0)
        current_dd = float(drawdown.get("current_drawdown_pct") or 0)
    except (TypeError, ValueError):
        return
    profile = _dominant_profile(time_in_profile)
    stats = memory["drawdown_recovery_stats"][profile]
    stats["last_drawdown_pct"] = round(max_dd, 2)
    closed_pnl = float((points or {}).get("closed_pnl_gbp") or 0)
    if max_dd >= 3.0 and current_dd <= max_dd * 0.5 and closed_pnl >= 0:
        count = int(stats.get("recovery_count") or 0) + 1
        stats["recovery_count"] = count
        prev_avg = float(stats.get("avg_recovery_sessions") or 0)
        stats["avg_recovery_sessions"] = round(((prev_avg * (count - 1)) + 1.0) / count, 2)


def _update_missed_opportunities(
    memory: dict[str, Any],
    *,
    session_review: dict[str, Any] | None,
    self_reflection: dict[str, Any] | None,
) -> None:
    stats = memory["missed_opportunity_stats"]
    reflection_flags = set((self_reflection or {}).get("reflection_flags") or [])
    session_flags = set((session_review or {}).get("session_flags") or [])
    summary = (session_review or {}).get("session_summary") or {}
    points = summary.get("points_summary") or {}
    total_trades = int(summary.get("total_trades") or 0)

    triggered = False
    reason = None
    if reflection_flags.intersection({"MISSED_OPPORTUNITY", "MISSED_PNL_OPPORTUNITY"}):
        triggered = True
        reason = "self_reflection_missed_opportunity"
    elif "UNDER_TRADING" in session_flags and float(points.get("closed_pnl_gbp") or 0) > 0:
        triggered = True
        reason = "profitable_conditions_low_trade_count"

    if triggered:
        stats["missed_opportunity_count"] = int(stats.get("missed_opportunity_count") or 0) + 1
        score = float(stats.get("missed_opportunity_score") or 0)
        stats["missed_opportunity_score"] = round(min(100.0, score + 10.0 + min(20, total_trades)), 2)
        stats["last_reason"] = reason


def _apply_session_observation(
    memory: dict[str, Any],
    *,
    session_review: dict[str, Any] | None,
    api_feed_health: dict[str, Any] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    self_reflection: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    summary = (session_review or {}).get("session_summary") or {}
    points = summary.get("points_summary") or {}
    win_rate = _session_win_rate_pct(points)
    if win_rate is None:
        win_rate = 50.0 if float(points.get("closed_pnl_gbp") or 0) >= 0 else 45.0

    weights = _profile_weights(summary.get("trades_by_strategy_profile"))
    vol_regime = _volatility_regime(summary.get("volatility_summary"))
    feed_regime = _feed_regime(summary.get("feed_health_summary"), api_feed_health)
    tod = _time_of_day_bucket(now)

    for profile in _PROFILES:
        key = f"{profile.lower()}_win_rate"
        profile_obs = win_rate if weights[profile] >= 0.2 else win_rate * 0.9 + _DEFAULT_WIN_RATE * 0.1
        memory[key] = _ema(float(memory.get(key) or _DEFAULT_WIN_RATE), profile_obs)
        _update_regime_rates(memory, "per_volatility_regime_performance", vol_regime, profile, profile_obs)
        _update_regime_rates(memory, "per_feed_health_regime_performance", feed_regime, profile, profile_obs)
        _update_regime_rates(memory, "per_time_of_day_performance", tod, profile, profile_obs)

    for row in trade_pipeline_health or []:
        epic = str(row.get("epic") or "")
        profile = str(row.get("active_strategy_profile") or "MOMENTUM").upper()
        if epic and profile in _PROFILES:
            _update_epic_performance(memory, epic, profile, win_rate)

    _update_drawdown_recovery(
        memory,
        drawdown=summary.get("drawdown_summary"),
        time_in_profile=summary.get("time_in_profile"),
        points=points,
    )
    _update_missed_opportunities(memory, session_review=session_review, self_reflection=self_reflection)
    memory["observation_count"] = int(memory.get("observation_count") or 0) + 1


@dataclass
class StrategyWeightingAdvice:
    recommended_bias: str
    bias_confidence: int
    bias_reason: str
    bias_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_bias": self.recommended_bias,
            "bias_confidence": int(self.bias_confidence),
            "bias_reason": self.bias_reason,
            "bias_flags": sorted(set(self.bias_flags)),
        }


def _build_weighting_advice(
    summary: dict[str, Any],
    *,
    session_review: dict[str, Any] | None,
    vol_regime: str,
    feed_regime: str,
) -> dict[str, Any]:
    win_rates = summary.get("win_rates") or {}
    vol_perf = summary.get("regime_performance", {}).get("volatility", {})
    feed_perf = summary.get("regime_performance", {}).get("feed_health", {})

    candidates: list[tuple[str, float, str, str]] = []

    scalp_high = float((vol_perf.get("high") or {}).get("SCALP") or win_rates.get("scalp_win_rate") or 0)
    if vol_regime == "high" and scalp_high >= 55:
        candidates.append(("SCALP", scalp_high, "scalp_win_rate strong in high volatility", "SCALP_STRONG_IN_HIGH_VOL"))

    mom_med = float((vol_perf.get("medium") or {}).get("MOMENTUM") or win_rates.get("momentum_win_rate") or 0)
    if vol_regime == "medium" and mom_med >= 55:
        candidates.append(
            ("MOMENTUM", mom_med, "momentum_win_rate strong in medium volatility", "MOMENTUM_STRONG_IN_MEDIUM_VOL")
        )

    swing_low = float((vol_perf.get("low") or {}).get("SWING") or win_rates.get("swing_win_rate") or 0)
    if vol_regime == "low" and swing_low >= 55:
        candidates.append(("SWING", swing_low, "swing_win_rate strong in low volatility", "SWING_STRONG_IN_LOW_VOL"))

    rot_deg = float((feed_perf.get("degraded") or {}).get("ROTATION") or win_rates.get("rotation_win_rate") or 0)
    if feed_regime == "degraded" and rot_deg >= 55:
        candidates.append(
            ("ROTATION", rot_deg, "rotation_win_rate strong during feed degradation", "ROTATION_STRONG_IN_DEGRADED_FEED")
        )

    # Penalise weak profiles in current regime
    flags: list[str] = []
    if vol_regime == "medium" and mom_med < 45:
        flags.append("MOMENTUM_WEAK_IN_CHOP")
    if vol_regime == "high" and scalp_high < 45:
        flags.append("SCALP_WEAK_IN_HIGH_VOL")

    if not candidates:
        dominant = _dominant_profile((session_review or {}).get("session_summary", {}).get("time_in_profile"))
        return StrategyWeightingAdvice(
            recommended_bias=dominant,
            bias_confidence=45,
            bias_reason="no dominant regime edge — default to session-dominant profile",
            bias_flags=flags or ["NEUTRAL_BIAS"],
        ).to_dict()

    candidates.sort(key=lambda x: x[1], reverse=True)
    bias, score, reason, flag = candidates[0]
    flags.append(flag)
    confidence = min(95, max(50, int(score)))
    return StrategyWeightingAdvice(
        recommended_bias=bias,
        bias_confidence=confidence,
        bias_reason=reason,
        bias_flags=flags,
    ).to_dict()


def build_strategy_performance_summary(
    *,
    session_review: dict[str, Any] | None = None,
    self_reflection: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update rolling memory and return performance summary (advisory)."""
    if _OVERRIDE_SUMMARY is not None:
        return dict(_OVERRIDE_SUMMARY)

    memory = _get_memory()
    _apply_session_observation(
        memory,
        session_review=session_review,
        api_feed_health=api_feed_health,
        trade_pipeline_health=trade_pipeline_health,
        self_reflection=self_reflection,
        now=now,
    )

    summary = {
        "win_rates": {
            "scalp_win_rate": float(memory["scalp_win_rate"]),
            "momentum_win_rate": float(memory["momentum_win_rate"]),
            "swing_win_rate": float(memory["swing_win_rate"]),
            "rotation_win_rate": float(memory["rotation_win_rate"]),
        },
        "regime_performance": {
            "volatility": copy.deepcopy(memory["per_volatility_regime_performance"]),
            "feed_health": copy.deepcopy(memory["per_feed_health_regime_performance"]),
        },
        "epic_performance": copy.deepcopy(memory["per_epic_strategy_performance"]),
        "time_of_day_performance": copy.deepcopy(memory["per_time_of_day_performance"]),
        "drawdown_recovery": copy.deepcopy(memory["drawdown_recovery_stats"]),
        "missed_opportunity_summary": copy.deepcopy(memory["missed_opportunity_stats"]),
        "observation_count": int(memory.get("observation_count") or 0),
        "hard_enforcement_epics_active": sum(1 for r in (hard_enforcement_decisions or []) if r.get("active")),
        "selector_epics": len(strategy_selector_advice or []),
        "transition_epics": len(strategy_transition_advice or []),
    }
    return summary


def build_strategy_weighting_advice(
    *,
    performance_summary: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    api_feed_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advisory strategy bias from performance memory."""
    if _OVERRIDE_WEIGHTING is not None:
        return dict(_OVERRIDE_WEIGHTING)

    summary = performance_summary or {}
    session_summary = (session_review or {}).get("session_summary") or {}
    vol_regime = _volatility_regime(session_summary.get("volatility_summary"))
    feed_regime = _feed_regime(session_summary.get("feed_health_summary"), api_feed_health)
    return _build_weighting_advice(summary, session_review=session_review, vol_regime=vol_regime, feed_regime=feed_regime)


def build_strategy_performance_bundle(
    *,
    session_review: dict[str, Any] | None = None,
    self_reflection: dict[str, Any] | None = None,
    strategy_selector_advice: list[dict[str, Any]] | None = None,
    strategy_transition_advice: list[dict[str, Any]] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
    api_feed_health: dict[str, Any] | None = None,
    trade_pipeline_health: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full Phase 4 bundle for GUI status."""
    summary = build_strategy_performance_summary(
        session_review=session_review,
        self_reflection=self_reflection,
        strategy_selector_advice=strategy_selector_advice,
        strategy_transition_advice=strategy_transition_advice,
        hard_enforcement_decisions=hard_enforcement_decisions,
        api_feed_health=api_feed_health,
        trade_pipeline_health=trade_pipeline_health,
        now=now,
    )
    weighting = build_strategy_weighting_advice(
        performance_summary=summary,
        session_review=session_review,
        api_feed_health=api_feed_health,
    )
    return {
        "strategy_performance_memory": summary,
        "strategy_weighting_advice": weighting,
    }
