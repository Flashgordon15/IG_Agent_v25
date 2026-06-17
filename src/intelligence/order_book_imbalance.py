"""
Order Book Imbalance (OBI) — high-speed ratio from Level-2 depth payloads.
"""

from __future__ import annotations

from cockpit.telemetry_schema import OrderBookDepthPayload


def compute_obi_ratio(payload: OrderBookDepthPayload | dict) -> float:
    """
    OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume).

    Returns a value in [-1.0, 1.0]. Positive = institutional buy stacking.
    """
    if isinstance(payload, dict):
        model = OrderBookDepthPayload.model_validate(payload)
    else:
        model = payload
    bid_vol = model.total_bid_size()
    ask_vol = model.total_ask_size()
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, (bid_vol - ask_vol) / total))


def obi_institutional_flag(ratio: float, *, threshold: float = 0.65) -> str:
    """Classify extreme institutional stacking states."""
    r = float(ratio)
    if r >= threshold:
        return "EXTREME_BUY_STACK"
    if r <= -threshold:
        return "EXTREME_SELL_STACK"
    if r >= 0.35:
        return "BUY_BIAS"
    if r <= -0.35:
        return "SELL_BIAS"
    return "BALANCED"
