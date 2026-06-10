"""
Resolve macro trading strictness profile into dynamic gate limits.

Profiles tune environment fitness floor and RSI boundary filters without
changing sizing, margin, or drawdown math.
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


@dataclass(frozen=True)
class StrictnessLimits:
    profile: str
    fitness_floor: float
    rsi_sell_min: float
    rsi_buy_max: float


def _config_dict(config: Any | None) -> dict[str, Any]:
    if config is None:
        from system.config_loader import get_config

        return get_config()
    if hasattr(config, "as_dict"):
        return config.as_dict()
    if isinstance(config, dict):
        return config
    return {}


def resolve_strictness(config: Any | None = None) -> StrictnessLimits:
    """Return active strictness limits from ``trading_strictness_profile``."""
    raw = str(_config_dict(config).get("trading_strictness_profile") or DEFAULT_PROFILE)
    profile = raw.strip().lower()
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    limits = PROFILES[profile]
    return StrictnessLimits(
        profile=profile,
        fitness_floor=float(limits["fitness_floor"]),
        rsi_sell_min=float(limits["rsi_sell_min"]),
        rsi_buy_max=float(limits["rsi_buy_max"]),
    )
