"""
Fluid autopilot position scaling — intelligence-regime unlock for multi-position yield.

When microstructure confidence is high and spread forecast is clear, raise per-epic
and global position caps above the configured base limits.
"""

from __future__ import annotations

from typing import Any

MOMENTUM_REGIMES = frozenset(
    {"MOMENTUM_UP", "MOMENTUM_DOWN", "SWEEP_BUY", "SWEEP_SELL", "ORDER_BLOCK"}
)


def autopilot_confidence_rating(
    *,
    micro_confidence: float,
    spread_z: float,
    throttle_factor: float,
    blocked: bool,
) -> float:
    """0–100 autopilot scaling score for cockpit HUD."""
    if blocked or throttle_factor >= 0.75:
        return 0.0
    penalty = min(40.0, max(0.0, spread_z * 8.0) + throttle_factor * 35.0)
    return max(0.0, min(100.0, micro_confidence * 100.0 - penalty))


def cockpit_autopilot_rating(
    epic: str,
    *,
    micro_confidence: float,
    spread_z: float,
    throttle_factor: float,
    blocked: bool,
    config: Any | None = None,
) -> tuple[float, dict[str, Any]]:
    """
    Session-adjusted autopilot rating for Flight Deck Panel C.

    Reflects active Tokyo / overnight floor modifiers and lot-scale band.
    """
    from intelligence.liquidity_wave import (
        effective_microstructure_confidence_floor,
        in_tokyo_momentum_window,
        overnight_volatility_size_multiplier,
    )

    base = autopilot_confidence_rating(
        micro_confidence=micro_confidence,
        spread_z=spread_z,
        throttle_factor=throttle_factor,
        blocked=blocked,
    )
    floor, floor_reason = effective_microstructure_confidence_floor(
        epic, config=config
    )
    floor_pct = floor * 100.0
    conf_pct = (
        float(micro_confidence) * 100.0
        if micro_confidence <= 1.0
        else float(micro_confidence)
    )
    size_mult, size_reason = overnight_volatility_size_multiplier(
        micro_confidence, epic=epic, config=config
    )

    session_boost = 0.0
    if floor_pct > 0 and conf_pct >= floor_pct:
        session_boost = min(12.0, (conf_pct - floor_pct) * 0.35)
    elif in_tokyo_momentum_window(config=config):
        session_boost = min(8.0, conf_pct * 0.08)

    adjusted = max(0.0, min(100.0, base + session_boost))
    meta = {
        "session_micro_floor_pct": round(floor_pct, 1),
        "session_floor_reason": floor_reason,
        "tokyo_momentum_active": in_tokyo_momentum_window(config=config),
        "overnight_size_multiplier": size_mult,
        "overnight_size_reason": size_reason,
        "autopilot_base_rating": round(base, 1),
    }
    return adjusted, meta


def intelligence_position_bonus(
    cfg: Any | None,
    epic: str,
) -> tuple[int, str, float]:
    """
    Return (epic_bonus_slots, reason, autopilot_confidence_rating).

    epic_bonus_slots added to base max_positions_per_epic when conditions met.
    """
    from intelligence.policy import autopilot_scaling_enabled

    if not autopilot_scaling_enabled(cfg):
        return 0, "autopilot off", 0.0

    ap = cfg.get("intelligence_layer", {}).get("autopilot_scaling", {})
    if not isinstance(ap, dict):
        from intelligence.policy import autopilot_config

        ap = autopilot_config(cfg)
    min_conf = float(ap.get("min_micro_confidence", 0.72))
    max_bonus = max(0, int(ap.get("max_epic_bonus", 2)))
    require_clear = bool(ap.get("require_clear_spread", True))
    max_throttle = float(ap.get("max_throttle_for_scale", 0.35))

    try:
        from intelligence.pipeline_bridge import get_intelligence_layer

        layer = get_intelligence_layer()
        spread = layer.spread_verdict(epic)
        micro = layer.microstructure_verdict(epic)
    except Exception:
        return 0, "intelligence unavailable", 0.0

    rating = autopilot_confidence_rating(
        micro_confidence=float(micro.confidence),
        spread_z=float(spread.z_score),
        throttle_factor=float(spread.throttle_factor),
        blocked=bool(spread.blocked),
    )

    if require_clear and (spread.blocked or spread.throttle_factor > max_throttle):
        return (
            0,
            f"spread turbulence z={spread.z_score:.1f} throttle={spread.throttle_factor:.2f}",
            rating,
        )

    if float(micro.confidence) < min_conf:
        return 0, f"micro conf {micro.confidence:.0%} < {min_conf:.0%}", rating

    if str(micro.regime) not in MOMENTUM_REGIMES:
        return 0, f"regime {micro.regime} not scalable", rating

    bonus = max(1, min(max_bonus, 1 + int(float(micro.confidence) * max_bonus)))
    return (
        bonus,
        f"autopilot {micro.regime} conf={micro.confidence:.0%} rating={rating:.0f}",
        rating,
    )


def effective_autopilot_max_per_epic(
    *,
    cfg: Any | None,
    epic: str,
    base_cap: int,
    ladder_cap: int,
    ladder_reason: str,
) -> tuple[int, str, float]:
    """Merge HEALTHY laddering cap with intelligence bonus."""
    bonus, bonus_reason, rating = intelligence_position_bonus(cfg, epic)
    if bonus <= 0:
        merged, reason = _apply_target_cap(ladder_cap, base_cap, ladder_reason, cfg=cfg)
        return merged, reason, rating
    merged = max(ladder_cap, base_cap + bonus)
    reason = f"{ladder_reason}; {bonus_reason}"
    try:
        from intelligence.liquidity_wave import resolve_liquidity_wave

        wave = resolve_liquidity_wave()
        if wave.autopilot_multiplier > 1.0 and str(bonus_reason).find("autopilot") >= 0:
            excess = max(0, merged - base_cap)
            boosted_excess = int(excess * wave.autopilot_multiplier)
            merged = base_cap + boosted_excess
            reason = f"{reason}; {wave.phase.value} x{wave.autopilot_multiplier:.1f}"
    except Exception:
        pass
    merged, reason = _apply_target_cap(merged, base_cap, reason, cfg=cfg)
    return merged, reason, rating


def _apply_target_cap(
    cap: int,
    base_cap: int,
    reason: str,
    *,
    cfg: Any | None,
) -> tuple[int, str]:
    try:
        from intelligence.target_engine import apply_target_position_cap

        return apply_target_position_cap(cap, base_cap, reason, cfg=cfg)
    except Exception:
        return cap, reason
