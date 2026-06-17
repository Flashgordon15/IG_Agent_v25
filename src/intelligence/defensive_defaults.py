"""Defensive defaults when markets are closed or caches are empty."""

from __future__ import annotations

from intelligence.types import MicroRegime, MicrostructureVerdict, SpreadForecastVerdict


def neutral_microstructure_verdict(epic: str = "", *, reason: str = "empty_cache") -> MicrostructureVerdict:
    return MicrostructureVerdict(
        epic=str(epic or ""),
        regime="NEUTRAL",
        confidence=0.35,
        momentum_5s=0.0,
        momentum_1m=0.0,
        momentum_5m=0.0,
        sweep_detected=False,
        order_block_detected=False,
        detail=reason,
    )


def neutral_spread_verdict(epic: str = "", *, reason: str = "insufficient_samples") -> SpreadForecastVerdict:
    return SpreadForecastVerdict(
        epic=str(epic or ""),
        spread=0.0,
        spread_delta=0.0,
        z_score=0.0,
        delta_z_score=0.0,
        mean_spread=0.0,
        std_spread=0.0,
        throttle_factor=0.0,
        offset_widen_pts=0.0,
        blocked=False,
        reason=reason,
    )
