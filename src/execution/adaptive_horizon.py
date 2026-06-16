"""
QMM adaptive execution horizon — scalping vs trend riding (in-memory classifier).

Uses live_state_vector from Gate 10; does not alter underlying indicator math.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdaptiveHorizonPlan:
    horizon: str  # "scalp" | "trend"
    trailing_distance_points: float
    trailing_trigger_points: float
    breakeven_trigger_points: float
    news_flow_sensitive: bool
    confidence: float
    notes: str

    def to_execution_overlay(self) -> dict[str, Any]:
        return {
            "qmm_horizon": self.horizon,
            "qmm_trailing_distance_points": self.trailing_distance_points,
            "qmm_trailing_trigger_points": self.trailing_trigger_points,
            "qmm_breakeven_trigger_points": self.breakeven_trigger_points,
            "qmm_news_flow_sensitive": self.news_flow_sensitive,
            "qmm_horizon_confidence": self.confidence,
            "qmm_horizon_notes": self.notes,
        }


def classify_execution_horizon(
    live_state_vector: dict[str, Any] | None,
    *,
    stop_points: float,
    cfg: Any | None = None,
) -> AdaptiveHorizonPlan:
    """
  Classify entry horizon from RAM features only.

  Scalp: tight range / mean-reversion (low ATR mult, young quote, moderate spread).
  Trend: structural breakout (high ATR mult, strong session score, sentiment tailwind).
    """
    vec = live_state_vector if isinstance(live_state_vector, dict) else {}
    atr_mult = float(vec.get("atr_multiplier") or 0.0)
    spread = float(vec.get("spread") or 0.0)
    quote_age_s = float(vec.get("quote_age_s") or 0.0)
    session_score = float(vec.get("session_score") or 0.0)

    qmm_cfg: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        raw = cfg.get("qmm_framework") or {}
        if isinstance(raw, dict):
            qmm_cfg = raw

    scalp_atr_max = float(qmm_cfg.get("scalp_atr_multiplier_max") or 0.45)
    trend_atr_min = float(qmm_cfg.get("trend_atr_multiplier_min") or 0.75)
    scalp_trail_mult = float(qmm_cfg.get("scalp_trailing_atr_mult") or 0.35)
    trend_trail_mult = float(qmm_cfg.get("trend_trailing_atr_mult") or 1.25)

    stop = max(1.0, float(stop_points or 40.0))
    range_like = atr_mult <= scalp_atr_max and spread > 0 and quote_age_s < 15.0
    breakout_like = atr_mult >= trend_atr_min and session_score >= 0.0

    if breakout_like and not range_like:
        horizon = "trend"
        trail_dist = max(8.0, stop * trend_trail_mult)
        trail_trig = max(6.0, stop * 0.5)
        be_trig = max(4.0, stop * 0.35)
        conf = min(1.0, 0.55 + atr_mult * 0.25)
        notes = "trend horizon — wide macro trail, news-flow sensitive"
        news_sensitive = True
    else:
        horizon = "scalp"
        trail_dist = max(3.0, stop * scalp_trail_mult)
        trail_trig = max(2.0, stop * 0.2)
        be_trig = max(2.0, stop * 0.15)
        conf = min(1.0, 0.5 + max(0.0, scalp_atr_max - atr_mult))
        notes = "scalp horizon — tight reactive trail for micro-mean-reversion"
        news_sensitive = False

    return AdaptiveHorizonPlan(
        horizon=horizon,
        trailing_distance_points=round(trail_dist, 2),
        trailing_trigger_points=round(trail_trig, 2),
        breakeven_trigger_points=round(be_trig, 2),
        news_flow_sensitive=news_sensitive,
        confidence=round(conf, 3),
        notes=notes,
    )
