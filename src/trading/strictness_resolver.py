"""
Resolve macro trading strictness profile into dynamic gate limits.

Profiles tune environment fitness floor and RSI boundary filters without
changing sizing, margin, or drawdown math.

Strictness is resolved from live **velocity regime** (ATR vs 14-day baseline +
trend cleanliness), not from a static dashboard toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROFILES: dict[str, dict[str, float]] = {
    "loose": {
        "fitness_floor": 30.0,
        "rsi_sell_min": 0.0,
        "rsi_buy_max": 100.0,
    },
    "firm": {
        "fitness_floor": 45.0,
        "rsi_sell_min": 15.0,
        "rsi_buy_max": 85.0,
    },
    "strict": {
        "fitness_floor": 55.0,
        "rsi_sell_min": 20.0,
        "rsi_buy_max": 80.0,
    },
}

DEFAULT_PROFILE = "firm"
VALID_PROFILES = frozenset(PROFILES.keys())

# Normalized vol = current ATR / mean ATR over ~14 calendar days of 5m bars.
VELOCITY_LOOSE_RATIO = 1.3
VELOCITY_STRICT_RATIO = 0.7
BARS_PER_DAY_5M = 288  # 24h × 12 bars/hour
BASELINE_LOOKBACK_BARS = 14 * BARS_PER_DAY_5M
TREND_CLEANLINESS_HIGH = 25.0  # full 15m EMA+RSI alignment (FACTOR_TREND_MAX)


@dataclass(frozen=True)
class VelocityMetrics:
    normalized_volatility: float
    trend_cleanliness: float


@dataclass(frozen=True)
class StrictnessLimits:
    profile: str
    fitness_floor: float
    rsi_sell_min: float
    rsi_buy_max: float
    velocity_regime: str = "firm"
    normalized_volatility: float | None = None
    trend_cleanliness: float | None = None


def _config_dict(config: Any | None) -> dict[str, Any]:
    if config is None:
        from system.config_loader import get_config

        return get_config()
    if hasattr(config, "as_dict"):
        return config.as_dict()
    if isinstance(config, dict):
        return config
    return {}


def compute_velocity_metrics(
    signal_engine: Any | None,
    market: str,
) -> VelocityMetrics | None:
    """
    Live velocity inputs for strictness morphing.

    normalized_volatility = current 5m ATR / mean 5m ATR over ~14 days.
    trend_cleanliness = direction-neutral 15m alignment score (0–25+).
    """
    if signal_engine is None:
        return None
    key = str(market or "").strip()
    if not key:
        return None
    try:
        df = signal_engine.quote_df(key)
        c5 = signal_engine.candles(df, 5)
        c15 = signal_engine.candles(df, 15)
        if len(c5) < 4 or len(c15) < 2:
            return None
        c5i = signal_engine.add_indicators(c5)
        c15i = signal_engine.add_indicators(c15)
        atr_series = c5i["atr"].dropna()
        if len(atr_series) < 20:
            return None
        current_atr = float(c5i.iloc[-2].get("atr", 0) or 0)
        if current_atr <= 0:
            return None
        lookback = min(len(atr_series), BASELINE_LOOKBACK_BARS)
        baseline_atr = float(atr_series.iloc[-lookback:].mean())
        if baseline_atr <= 0:
            return None
        norm_vol = current_atr / baseline_atr

        from runtime.market_orchestrator import compute_rotation_trend_cleanliness

        row15 = c15i.iloc[-2]
        atr_15 = float(row15.get("atr", 0) or 0)
        atr_5 = current_atr
        trend_clean = compute_rotation_trend_cleanliness(
            row15, atr_15m=atr_15, atr_5m=atr_5
        )
        return VelocityMetrics(
            normalized_volatility=norm_vol,
            trend_cleanliness=trend_clean,
        )
    except Exception:
        return None


def _profile_from_velocity(metrics: VelocityMetrics | None) -> str:
    if metrics is None:
        return DEFAULT_PROFILE
    nv = float(metrics.normalized_volatility)
    tc = float(metrics.trend_cleanliness)
    if nv > VELOCITY_LOOSE_RATIO and tc >= TREND_CLEANLINESS_HIGH:
        return "loose"
    if nv < VELOCITY_STRICT_RATIO:
        return "strict"
    return "firm"


def resolve_strictness(
    config: Any | None = None,
    *,
    velocity_metrics: VelocityMetrics | None = None,
    signal_engine: Any | None = None,
    market: str | None = None,
) -> StrictnessLimits:
    """Return strictness limits from live velocity regime (per instrument)."""
    if velocity_metrics is None and signal_engine is not None and market:
        velocity_metrics = compute_velocity_metrics(signal_engine, market)
    profile = _profile_from_velocity(velocity_metrics)
    limits = PROFILES[profile]
    return StrictnessLimits(
        profile=profile,
        fitness_floor=float(limits["fitness_floor"]),
        rsi_sell_min=float(limits["rsi_sell_min"]),
        rsi_buy_max=float(limits["rsi_buy_max"]),
        velocity_regime=profile,
        normalized_volatility=(
            float(velocity_metrics.normalized_volatility)
            if velocity_metrics is not None
            else None
        ),
        trend_cleanliness=(
            float(velocity_metrics.trend_cleanliness)
            if velocity_metrics is not None
            else None
        ),
    )


def is_low_volatility_regime(
    *,
    signal_engine: Any | None = None,
    market: str | None = None,
) -> bool:
    """True when normalized ATR velocity is below the strict-regime threshold (<0.7)."""
    mkt = str(market or "").strip()
    engine = signal_engine
    if engine is None and mkt:
        try:
            from runtime.market_orchestrator import MarketOrchestrator

            engine = MarketOrchestrator.get_signal_engine_for_market(mkt)
        except Exception:
            return False
    if engine is None or not mkt:
        return False
    try:
        metrics = compute_velocity_metrics(engine, mkt)
        if metrics is None:
            return False
        return float(metrics.normalized_volatility) < VELOCITY_STRICT_RATIO
    except Exception:
        return False


def strictness_payload(config: Any | None = None) -> dict[str, Any]:
    """API/dashboard view — velocity-driven strictness (no static profile toggle)."""
    limits = resolve_strictness(config)
    return {
        "mode": "velocity_regime",
        "profile": limits.profile,
        "velocity_regime": limits.velocity_regime,
        "fitness_floor": limits.fitness_floor,
        "rsi_sell_min": limits.rsi_sell_min,
        "rsi_buy_max": limits.rsi_buy_max,
        "normalized_volatility": limits.normalized_volatility,
        "trend_cleanliness": limits.trend_cleanliness,
        "profiles": PROFILES,
        "default_profile": DEFAULT_PROFILE,
        "velocity_loose_ratio": VELOCITY_LOOSE_RATIO,
        "velocity_strict_ratio": VELOCITY_STRICT_RATIO,
        "trend_cleanliness_high": TREND_CLEANLINESS_HIGH,
        "note": (
            "Strictness morphs per instrument from live ATR velocity and "
            "15m trend alignment; dashboard profile toggle is deprecated."
        ),
    }


def set_strictness_profile(profile: str, *, hot_reload: bool = True) -> dict[str, Any]:
    """
    Deprecated — strictness is velocity-driven at runtime.

    Kept for API compatibility; does not change live gate limits.
    """
    key = str(profile or "").strip().lower()
    if key not in VALID_PROFILES:
        raise ValueError(
            f"Invalid trading_strictness_profile {profile!r} — use loose, firm, or strict"
        )

    from system.config_loader import get_config
    from system.engine_log import log_engine

    log_engine(
        f"strictness profile toggle ignored — velocity regime active "
        f"(requested {key!r}, hot_reload={hot_reload})"
    )
    payload = strictness_payload(get_config())
    payload["requested_profile"] = key
    payload["deprecated"] = True
    return payload
