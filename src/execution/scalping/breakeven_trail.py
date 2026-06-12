"""Scalping breakeven milestone and ATR trailing helpers."""

from __future__ import annotations

from typing import Any

from data.models import Quote
from execution.execution_protect import protect_settings as _protect_settings


def _settings(cfg: Any | None = None) -> dict[str, Any]:
    return _protect_settings(cfg)


def spread_points(quote: Quote) -> float:
    try:
        return max(0.0, float(quote.offer) - float(quote.bid))
    except (TypeError, ValueError):
        return 0.0


def commission_points(cfg: Any | None = None) -> float:
    s = _settings(cfg)
    per_side = float(s.get("commission_points_per_side", 0.5))
    return max(0.0, per_side * 2.0)


def breakeven_trigger_points(quote: Quote, cfg: Any | None = None) -> float:
    """Spread + commissions + buffer (default 2 pts)."""
    s = _settings(cfg)
    buffer_pts = float(s.get("breakeven_buffer_points", 2.0))
    return spread_points(quote) + commission_points(cfg) + buffer_pts


def breakeven_stop_offset(quote: Quote, cfg: Any | None = None) -> float:
    """Lock stop at entry plus round-trip transaction costs."""
    return commission_points(cfg) + spread_points(quote) * 0.5


def trail_distance_from_atr(atr: float, cfg: Any | None = None) -> float:
    s = _settings(cfg)
    mult = float(s.get("atr_trail_multiplier", 0.5))
    try:
        atr_v = float(atr)
    except (TypeError, ValueError):
        return 0.0
    if atr_v <= 0:
        return 0.0
    return mult * atr_v


def resolve_atr_14(snapshot: dict[str, Any] | None) -> float:
    if not snapshot:
        return 0.0
    last = snapshot.get("last")
    if last is None:
        return 0.0
    try:
        if hasattr(last, "get"):
            return float(last.get("atr", 0) or 0)
        return float(last["atr"])
    except (TypeError, ValueError, KeyError):
        return 0.0
