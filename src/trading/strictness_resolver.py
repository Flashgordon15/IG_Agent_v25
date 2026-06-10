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
VALID_PROFILES = frozenset(PROFILES.keys())


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


def strictness_payload(config: Any | None = None) -> dict[str, Any]:
    """API/dashboard view of the active profile and all preset limits."""
    limits = resolve_strictness(config)
    return {
        "profile": limits.profile,
        "fitness_floor": limits.fitness_floor,
        "rsi_sell_min": limits.rsi_sell_min,
        "rsi_buy_max": limits.rsi_buy_max,
        "profiles": PROFILES,
        "default_profile": DEFAULT_PROFILE,
    }


def set_strictness_profile(profile: str, *, hot_reload: bool = True) -> dict[str, Any]:
    """
    Persist ``trading_strictness_profile`` to the primary config tier and reload runtime.

    Patches only the active config file (e.g. config_v29.json) — preserves ``$extends``.
    """
    key = str(profile or "").strip().lower()
    if key not in VALID_PROFILES:
        raise ValueError(
            f"Invalid trading_strictness_profile {profile!r} — use loose, firm, or strict"
        )

    from system.config_loader import _primary_config_path, get_config
    from system.engine_log import log_engine

    path = _primary_config_path()
    if not path.is_file():
        raise FileNotFoundError(f"Primary config not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be an object: {path}")

    previous = str(data.get("trading_strictness_profile") or DEFAULT_PROFILE).lower()
    data["trading_strictness_profile"] = key
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

    cfg = get_config(reload=True)
    reloaded_loops = 0
    if hot_reload:
        from runtime.market_orchestrator import MarketOrchestrator

        reloaded_loops = MarketOrchestrator.hot_reload_config(cfg)

    log_engine(
        f"strictness profile {previous!r} → {key!r} "
        f"(fitness>={resolve_strictness(cfg).fitness_floor:.0f}%, "
        f"hot_reload_loops={reloaded_loops})"
    )
    payload = strictness_payload(cfg)
    payload["previous_profile"] = previous
    payload["hot_reload_loops"] = reloaded_loops
    payload["config_path"] = str(path)
    return payload
