"""
Autonomous strategy governance — Phase 11 long-horizon advisory layer (v41).

Adjusts strategy thresholds, biases, and preferences over time. Advisory-only.
Does NOT modify trading, execution, sizing, or config.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from runtime.adaptive_thresholds import BASELINE_THRESHOLDS
from runtime.regime_detection import MarketRegime

_PROFILES = ("SCALP", "MOMENTUM", "SWING", "ROTATION")
_PROFILE_THRESHOLD_KEYS = {
    "SCALP": "SCALP_CONFIDENCE_THRESHOLD",
    "MOMENTUM": "MOMENTUM_CONFIDENCE_THRESHOLD",
    "SWING": "SWING_CONFIDENCE_THRESHOLD",
    "ROTATION": "ROTATION_SCALP_OVERRIDE_THRESHOLD",
}
_PROFILE_PRIMARY_PATH = {
    "SCALP": "MICRO",
    "MOMENTUM": "PATH_A",
    "SWING": "PATH_A",
    "ROTATION": "PATH_B_HANDOFF",
}
_STRONG_WIN_RATE = 58.0
_REGIME_PERSISTENCE_MIN = 3
_ENFORCEMENT_CONFLICT_MIN = 1.5

_STATE: dict[str, Any] = {}
_OVERRIDE: dict[str, Any] | None = None


@dataclass
class StrategyGovernance:
    governance_adjustments: dict[str, Any]
    governance_confidence: int
    governance_reason: str
    governance_flags: list[str] = field(default_factory=list)
    contributing_factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "governance_adjustments": copy.deepcopy(self.governance_adjustments),
            "governance_confidence": int(self.governance_confidence),
            "governance_reason": self.governance_reason,
            "governance_flags": sorted(set(self.governance_flags)),
            "contributing_factors": dict(self.contributing_factors),
        }


def reset_strategy_governance_for_tests() -> None:
    global _STATE, _OVERRIDE
    _STATE = {}
    _OVERRIDE = None


def set_strategy_governance_for_tests(payload: dict[str, Any] | None) -> None:
    global _OVERRIDE
    _OVERRIDE = payload


def _empty_state() -> dict[str, Any]:
    return {
        "regime_observations": [],
        "daily_progress_ratios": [],
        "enforcement_active_samples": [],
        "enforcement_blocked_profile_samples": [],
        "session_stability_samples": [],
        "session_risk_samples": [],
        "drawdown_pct_samples": [],
        "observation_count": 0,
    }


def _get_state() -> dict[str, Any]:
    global _STATE
    if not _STATE:
        _STATE = _empty_state()
    return _STATE


def _dominant_regime(regime_detection: list[dict[str, Any]] | None) -> str:
    if not regime_detection:
        return MarketRegime.UNKNOWN.value
    counts: dict[str, int] = {}
    for row in regime_detection:
        regime = str(row.get("regime_classification") or MarketRegime.UNKNOWN.value).upper()
        counts[regime] = counts.get(regime, 0) + 1
    if not counts:
        return MarketRegime.UNKNOWN.value
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _win_rates(performance_memory: dict[str, Any] | None) -> dict[str, float]:
    rates = (performance_memory or {}).get("win_rates") or {}
    return {
        p: float(rates.get(f"{p.lower()}_win_rate") or 50.0)
        for p in _PROFILES
    }


def _strongest_profile(win_rates: dict[str, float]) -> tuple[str, float]:
    best = "MOMENTUM"
    best_rate = -1.0
    for profile, rate in win_rates.items():
        if rate > best_rate:
            best_rate = rate
            best = profile
    return best, best_rate


def _regime_persistence_counts(observations: list[str]) -> dict[str, int]:
    if not observations:
        return {}
    tail = observations[-10:]
    counts: dict[str, int] = {}
    for regime in tail:
        counts[regime] = counts.get(regime, 0) + 1
    return counts


def _empty_adjustments() -> dict[str, Any]:
    return {
        "strategy_bias_adjustments": {p: 0.0 for p in _PROFILES},
        "threshold_adjustments": {},
        "risk_bias_adjustments": {"tighten": 0.0, "loosen": 0.0},
        "sizing_bias_adjustments": {"increase": 0.0, "decrease": 0.0},
        "regime_sensitivity_adjustments": {r.value: 0.0 for r in MarketRegime if r != MarketRegime.UNKNOWN},
        "stand_down_sensitivity_adjustments": 0.0,
    }


def _raise_threshold(adjustments: dict[str, Any], key: str, delta: float) -> None:
    adjustments["threshold_adjustments"][key] = (
        adjustments["threshold_adjustments"].get(key, 0) + delta
    )


def _blocked_profiles_from_decision(decision: dict[str, Any]) -> list[str]:
    """Derive profiles whose primary execution path is hard-blocked."""
    if not decision.get("active"):
        return []

    flags = [str(f) for f in (decision.get("enforcement_flags") or [])]
    if "STAND_DOWN_HARD" in flags:
        return list(_PROFILES)

    blocked_paths = {str(p) for p in (decision.get("hard_block_paths") or [])}
    blocked: set[str] = set()

    for profile, path in _PROFILE_PRIMARY_PATH.items():
        if path in blocked_paths:
            blocked.add(profile)

    for key in ("blocked_profile", "blocked_profiles"):
        raw = decision.get(key)
        if not raw:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for val in values:
            profile = str(val).upper()
            if profile in _PROFILES:
                blocked.add(profile)

    active_profile = decision.get("active_profile") or decision.get("ownership")
    if active_profile:
        active_u = str(active_profile).upper()
        for profile in _PROFILES:
            if profile != active_u:
                path = _PROFILE_PRIMARY_PATH[profile]
                if path in blocked_paths:
                    blocked.add(profile)

    return sorted(blocked)


def _blocked_profile_counts(decisions: list[dict[str, Any]] | None) -> dict[str, int]:
    counts = {p: 0 for p in _PROFILES}
    for row in decisions or []:
        for profile in _blocked_profiles_from_decision(row):
            counts[profile] += 1
    return counts


def _progress_stability_confidence(ratios: list[float]) -> float:
    if len(ratios) < 2:
        return 50.0 if ratios else 0.0
    avg = sum(ratios) / len(ratios)
    variance = sum((r - avg) ** 2 for r in ratios) / len(ratios)
    stddev = variance ** 0.5
    return max(0.0, min(100.0, (1.0 - stddev) * 100.0))


def _compute_governance_confidence(
    *,
    strongest_rate: float,
    persistence: dict[str, int],
    observation_count: int,
    avg_drawdown: float,
    progress_history: list[float],
    enforcement_samples: list[int],
) -> tuple[int, dict[str, float]]:
    long_term = max(0.0, min(100.0, (strongest_rate - 50.0) * 5.0))

    tail_len = min(10, max(1, observation_count))
    max_persist = max(persistence.values()) if persistence else 0
    regime_persist = max(0.0, min(100.0, (max_persist / tail_len) * 100.0))

    drawdown_cycle = max(0.0, min(100.0, (1.0 - avg_drawdown / 10.0) * 100.0))

    daily_target = _progress_stability_confidence(progress_history)

    avg_enforcement = (
        sum(enforcement_samples) / len(enforcement_samples) if enforcement_samples else 0.0
    )
    enforcement_history = max(0.0, min(100.0, (1.0 - avg_enforcement / 4.0) * 100.0))

    components = {
        "long_term_performance_confidence": round(long_term, 1),
        "regime_persistence_confidence": round(regime_persist, 1),
        "drawdown_cycle_confidence": round(drawdown_cycle, 1),
        "daily_target_history_confidence": round(daily_target, 1),
        "enforcement_history_confidence": round(enforcement_history, 1),
    }

    weighted = (
        0.35 * long_term
        + 0.25 * regime_persist
        + 0.20 * drawdown_cycle
        + 0.10 * daily_target
        + 0.10 * enforcement_history
    )
    return int(max(0, min(100, round(weighted)))), components


def _update_state(
    state: dict[str, Any],
    *,
    regime_detection: list[dict[str, Any]] | None,
    daily_pnl_targeting: dict[str, Any] | None,
    hard_enforcement_decisions: list[dict[str, Any]] | None,
    session_review: dict[str, Any] | None,
) -> None:
    dominant = _dominant_regime(regime_detection)
    state["regime_observations"].append(dominant)
    state["regime_observations"] = state["regime_observations"][-20:]

    try:
        progress = float((daily_pnl_targeting or {}).get("progress_ratio") or 0)
    except (TypeError, ValueError):
        progress = 0.0
    state["daily_progress_ratios"].append(progress)
    state["daily_progress_ratios"] = state["daily_progress_ratios"][-20:]

    active = sum(1 for r in (hard_enforcement_decisions or []) if r.get("active"))
    state["enforcement_active_samples"].append(active)
    state["enforcement_active_samples"] = state["enforcement_active_samples"][-20:]

    blocked_counts = _blocked_profile_counts(hard_enforcement_decisions)
    state["enforcement_blocked_profile_samples"].append(blocked_counts)
    state["enforcement_blocked_profile_samples"] = state["enforcement_blocked_profile_samples"][-20:]

    stability = int((session_review or {}).get("session_stability_score") or 50)
    risk = int((session_review or {}).get("session_risk_score") or 30)
    state["session_stability_samples"].append(stability)
    state["session_risk_samples"].append(risk)
    state["session_stability_samples"] = state["session_stability_samples"][-20:]
    state["session_risk_samples"] = state["session_risk_samples"][-20:]

    drawdown = ((session_review or {}).get("session_summary") or {}).get("drawdown_summary") or {}
    try:
        dd_pct = float(drawdown.get("max_drawdown_pct") or 0)
    except (TypeError, ValueError):
        dd_pct = 0.0
    state["drawdown_pct_samples"].append(dd_pct)
    state["drawdown_pct_samples"] = state["drawdown_pct_samples"][-20:]

    state["observation_count"] = int(state.get("observation_count") or 0) + 1


def build_strategy_governance(
    *,
    strategy_performance_memory: dict[str, Any] | None = None,
    adaptive_thresholds: dict[str, Any] | None = None,
    regime_detection: list[dict[str, Any]] | None = None,
    regime_aware_strategy_selector: list[dict[str, Any]] | None = None,
    regime_risk_envelope: list[dict[str, Any]] | None = None,
    regime_sizing_advice: list[dict[str, Any]] | None = None,
    daily_pnl_targeting: dict[str, Any] | None = None,
    session_review: dict[str, Any] | None = None,
    hard_enforcement_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build long-horizon strategy governance advisory payload."""
    if _OVERRIDE is not None:
        return dict(_OVERRIDE)

    state = _get_state()
    _update_state(
        state,
        regime_detection=regime_detection,
        daily_pnl_targeting=daily_pnl_targeting,
        hard_enforcement_decisions=hard_enforcement_decisions,
        session_review=session_review,
    )

    adjustments = _empty_adjustments()
    flags: list[str] = []
    reasons: list[str] = []

    win_rates = _win_rates(strategy_performance_memory)
    strongest, strongest_rate = _strongest_profile(win_rates)

    if strongest == "SCALP" and strongest_rate >= _STRONG_WIN_RATE:
        adjustments["strategy_bias_adjustments"]["SCALP"] += 0.15
        adjustments["threshold_adjustments"]["SCALP_CONFIDENCE_THRESHOLD"] = -3.0
        flags.append("LONG_TERM_SCALP_BIAS")
        reasons.append(f"SCALP win rate {strongest_rate:.0f}% leads long-term performance")
    elif strongest == "MOMENTUM" and strongest_rate >= _STRONG_WIN_RATE:
        adjustments["strategy_bias_adjustments"]["MOMENTUM"] += 0.15
        adjustments["threshold_adjustments"]["MOMENTUM_CONFIDENCE_THRESHOLD"] = -3.0
        flags.append("LONG_TERM_MOMENTUM_BIAS")
        reasons.append(f"MOMENTUM win rate {strongest_rate:.0f}% leads long-term performance")
    elif strongest == "SWING" and strongest_rate >= _STRONG_WIN_RATE:
        adjustments["strategy_bias_adjustments"]["SWING"] += 0.15
        adjustments["threshold_adjustments"]["SWING_CONFIDENCE_THRESHOLD"] = -3.0
        flags.append("LONG_TERM_SWING_BIAS")
        reasons.append(f"SWING win rate {strongest_rate:.0f}% leads long-term performance")
    elif strongest == "ROTATION" and strongest_rate >= _STRONG_WIN_RATE:
        adjustments["strategy_bias_adjustments"]["ROTATION"] += 0.15
        adjustments["threshold_adjustments"]["ROTATION_SCALP_OVERRIDE_THRESHOLD"] = -3.0
        flags.append("LONG_TERM_ROTATION_BIAS")
        reasons.append(f"ROTATION win rate {strongest_rate:.0f}% leads long-term performance")

    persistence = _regime_persistence_counts(state["regime_observations"])
    if persistence.get(MarketRegime.TREND.value, 0) >= _REGIME_PERSISTENCE_MIN:
        adjustments["strategy_bias_adjustments"]["MOMENTUM"] += 0.10
        adjustments["strategy_bias_adjustments"]["SCALP"] -= 0.08
        adjustments["regime_sensitivity_adjustments"][MarketRegime.TREND.value] += 0.10
        flags.append("REGIME_PERSISTENCE_TREND")
        reasons.append("TREND regime persistent — MOMENTUM up, SCALP down")
    if persistence.get(MarketRegime.CHOP.value, 0) >= _REGIME_PERSISTENCE_MIN:
        adjustments["strategy_bias_adjustments"]["SCALP"] += 0.10
        adjustments["strategy_bias_adjustments"]["MOMENTUM"] -= 0.08
        adjustments["regime_sensitivity_adjustments"][MarketRegime.CHOP.value] += 0.10
        flags.append("REGIME_PERSISTENCE_CHOP")
        reasons.append("CHOP regime persistent — SCALP up, MOMENTUM down")
    if persistence.get(MarketRegime.REVERSAL.value, 0) >= _REGIME_PERSISTENCE_MIN:
        adjustments["strategy_bias_adjustments"]["ROTATION"] += 0.10
        adjustments["strategy_bias_adjustments"]["SWING"] -= 0.08
        flags.append("REGIME_PERSISTENCE_REVERSAL")
        reasons.append("REVERSAL regime persistent — ROTATION up, SWING down")
    if persistence.get(MarketRegime.LOW_VOL.value, 0) >= _REGIME_PERSISTENCE_MIN:
        adjustments["strategy_bias_adjustments"]["SWING"] += 0.08
        adjustments["strategy_bias_adjustments"]["SCALP"] -= 0.06
        flags.append("REGIME_PERSISTENCE_LOW_VOL")
        reasons.append("LOW_VOL regime persistent — SWING up, SCALP down")

    avg_drawdown = (
        sum(state["drawdown_pct_samples"]) / len(state["drawdown_pct_samples"])
        if state["drawdown_pct_samples"]
        else 0.0
    )
    if avg_drawdown >= 4.0:
        adjustments["stand_down_sensitivity_adjustments"] += 5.0
        adjustments["risk_bias_adjustments"]["tighten"] += 0.10
        adjustments["sizing_bias_adjustments"]["decrease"] += 0.10
        _raise_threshold(adjustments, "SOFT_BLOCK_THRESHOLD", 2.0)
        flags.append("DRAWDOWN_CYCLE_PROTECTION")
        reasons.append(f"elevated drawdown cycle avg {avg_drawdown:.1f}% — protective governance")

    progress_history = state["daily_progress_ratios"]
    if progress_history:
        avg_progress = sum(progress_history) / len(progress_history)
        if avg_progress < 0.30:
            adjustments["sizing_bias_adjustments"]["increase"] += 0.12
            adjustments["risk_bias_adjustments"]["loosen"] += 0.08
            adjustments["stand_down_sensitivity_adjustments"] -= 3.0
            for profile in _PROFILES:
                adjustments["strategy_bias_adjustments"][profile] += 0.03
            flags.append("TARGET_HISTORY_BEHIND")
            reasons.append("daily target history consistently behind — loosen governance")
        elif avg_progress >= 0.75:
            adjustments["stand_down_sensitivity_adjustments"] += 4.0
            adjustments["sizing_bias_adjustments"]["decrease"] += 0.08
            adjustments["risk_bias_adjustments"]["tighten"] += 0.06
            for profile in _PROFILES:
                adjustments["strategy_bias_adjustments"][profile] -= 0.04
            flags.append("TARGET_HISTORY_AHEAD")
            reasons.append("daily target history ahead — protective governance")

    blocked_samples = state["enforcement_blocked_profile_samples"]
    if blocked_samples:
        avg_blocked: dict[str, float] = {p: 0.0 for p in _PROFILES}
        for sample in blocked_samples:
            for profile in _PROFILES:
                avg_blocked[profile] += float(sample.get(profile) or 0)
        for profile in _PROFILES:
            avg_blocked[profile] /= len(blocked_samples)

        conflict_profiles = [
            p for p in _PROFILES if avg_blocked[p] >= _ENFORCEMENT_CONFLICT_MIN
        ]
        if conflict_profiles:
            adjustments["stand_down_sensitivity_adjustments"] += 3.0
            flags.append("ENFORCEMENT_CONFLICT_HISTORY")
            reasons.append(
                "hard enforcement frequently blocks profiles — "
                + ", ".join(conflict_profiles)
            )
            for profile in conflict_profiles:
                adjustments["strategy_bias_adjustments"][profile] -= 0.10
                threshold_key = _PROFILE_THRESHOLD_KEYS[profile]
                _raise_threshold(adjustments, threshold_key, 2.0)

    stability_samples = state["session_stability_samples"]
    risk_samples = state["session_risk_samples"]
    avg_stability = sum(stability_samples) / len(stability_samples) if stability_samples else 50.0
    avg_risk = sum(risk_samples) / len(risk_samples) if risk_samples else 30.0
    if avg_stability < 55 or avg_risk >= 55:
        _raise_threshold(adjustments, "SOFT_BLOCK_THRESHOLD", 2.0)
        adjustments["risk_bias_adjustments"]["tighten"] += 0.05
        adjustments["sizing_bias_adjustments"]["decrease"] += 0.06
        adjustments["stand_down_sensitivity_adjustments"] += 3.0
        flags.append("SESSION_INSTABILITY_TIGHTEN")
        reasons.append("multi-session instability/risk — tighten governance thresholds")

    existing_thresholds = (adaptive_thresholds or {}).get("threshold_adjustments") or {}
    for key, delta in list(adjustments["threshold_adjustments"].items()):
        baseline = BASELINE_THRESHOLDS.get(key)
        if baseline is not None:
            current = float(existing_thresholds.get(key) or baseline)
            adjustments["threshold_adjustments"][key] = round(current + delta - baseline, 2)

    if (adaptive_thresholds or {}).get("adjustment_flags"):
        flags.append("ADAPTIVE_THRESHOLD_CONTEXT")

    avg_sizing = 0.25
    sizing_rows = regime_sizing_advice or []
    if sizing_rows:
        avg_sizing = sum(float(r.get("recommended_size_factor") or 0) for r in sizing_rows) / len(sizing_rows)
    if avg_sizing < 0.15:
        adjustments["sizing_bias_adjustments"]["increase"] += 0.05
        flags.append("LOW_SIZING_HISTORY_BIAS")

    dominant_selector = "MOMENTUM"
    if regime_aware_strategy_selector:
        counts: dict[str, int] = {}
        for row in regime_aware_strategy_selector:
            profile = str(row.get("recommended_profile") or "").upper()
            counts[profile] = counts.get(profile, 0) + 1
        if counts:
            dominant_selector = max(counts.items(), key=lambda kv: kv[1])[0]
            adjustments["strategy_bias_adjustments"][dominant_selector] = (
                adjustments["strategy_bias_adjustments"].get(dominant_selector, 0) + 0.05
            )

    risk_profiles = [str(r.get("risk_profile") or "MEDIUM").upper() for r in (regime_risk_envelope or [])]
    if risk_profiles.count("TIGHT") >= max(1, len(risk_profiles) // 2):
        adjustments["risk_bias_adjustments"]["tighten"] += 0.05
        flags.append("RISK_ENVELOPE_HISTORY_TIGHT")

    enforcement_samples = state["enforcement_active_samples"]
    confidence, confidence_components = _compute_governance_confidence(
        strongest_rate=strongest_rate,
        persistence=persistence,
        observation_count=state["observation_count"],
        avg_drawdown=avg_drawdown,
        progress_history=progress_history,
        enforcement_samples=enforcement_samples,
    )

    if not reasons:
        reasons.append("baseline governance — no long-horizon adjustments triggered")

    avg_blocked_report: dict[str, float] = {p: 0.0 for p in _PROFILES}
    if blocked_samples:
        for sample in blocked_samples:
            for profile in _PROFILES:
                avg_blocked_report[profile] += float(sample.get(profile) or 0)
        for profile in _PROFILES:
            avg_blocked_report[profile] /= len(blocked_samples)

    factors = {
        "long_term_performance": win_rates,
        "regime_persistence": persistence,
        "drawdown_cycles": {
            "avg_drawdown_pct": round(avg_drawdown, 2),
            "samples": len(state["drawdown_pct_samples"]),
        },
        "daily_target_history": {
            "avg_progress_ratio": round(sum(progress_history) / len(progress_history), 3) if progress_history else 0,
            "samples": len(progress_history),
        },
        "enforcement_history": {
            "avg_active_epics": round(sum(enforcement_samples) / len(enforcement_samples), 2)
            if enforcement_samples
            else 0,
            "avg_blocked_profiles": {p: round(avg_blocked_report[p], 2) for p in _PROFILES},
            "samples": len(enforcement_samples),
        },
        "session_stability": {
            "avg_stability": round(avg_stability, 1),
            "avg_risk": round(avg_risk, 1),
            "samples": len(stability_samples),
        },
        "governance_confidence_components": confidence_components,
        "dominant_selector_profile": dominant_selector,
        "observation_count": state["observation_count"],
    }

    result = StrategyGovernance(
        governance_adjustments=adjustments,
        governance_confidence=confidence,
        governance_reason="; ".join(reasons[:4]),
        governance_flags=flags,
        contributing_factors=factors,
    )
    return result.to_dict()
