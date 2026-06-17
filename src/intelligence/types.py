"""Shared verdict types for the v29.1 intelligence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MicroRegime = Literal[
    "NEUTRAL",
    "MOMENTUM_UP",
    "MOMENTUM_DOWN",
    "SWEEP_BUY",
    "SWEEP_SELL",
    "ORDER_BLOCK",
]


@dataclass(frozen=True)
class SpreadForecastVerdict:
    epic: str
    spread: float
    spread_delta: float
    z_score: float
    delta_z_score: float
    mean_spread: float
    std_spread: float
    throttle_factor: float
    offset_widen_pts: float
    blocked: bool
    reason: str = ""


@dataclass(frozen=True)
class MicrostructureVerdict:
    epic: str
    regime: MicroRegime
    confidence: float
    momentum_5s: float
    momentum_1m: float
    momentum_5m: float
    sweep_detected: bool
    order_block_detected: bool
    detail: str = ""


@dataclass(frozen=True)
class AlphaTrailVerdict:
    epic: str
    side: str
    proposed_stop: float | None
    trail_distance_pts: float
    atr_multiple: float
    profit_pts: float
    tighten_mode: bool
    detail: str = ""
    deal_id: str = ""


@dataclass
class IntelligenceSnapshot:
    """Latest per-epic intelligence outputs — read by pipeline plugins."""

    spread: dict[str, SpreadForecastVerdict] = field(default_factory=dict)
    microstructure: dict[str, MicrostructureVerdict] = field(default_factory=dict)
    updated_at: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spread": {k: v.__dict__ for k, v in self.spread.items()},
            "microstructure": {k: v.__dict__ for k, v in self.microstructure.items()},
            "updated_at": dict(self.updated_at),
        }
