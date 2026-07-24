"""
Order Book Imbalance (OBI) — high-speed ratio from Level-2 depth payloads.
"""

from __future__ import annotations

from typing import Any

from cockpit.telemetry_schema import OrderBookDepthPayload


def compute_obi_ratio(payload: OrderBookDepthPayload | dict) -> float:
    """
    OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume).

    Returns a value in [-1.0, 1.0]. Positive = institutional buy stacking.
    Empty / zero-volume books return 0.0 (caller must treat missing books
    separately via ``compute_obi_ratio_available``).
    """
    ratio, _available = compute_obi_ratio_available(payload)
    return ratio


def compute_obi_ratio_available(
    payload: OrderBookDepthPayload | dict | None,
) -> tuple[float, bool]:
    """
    Return ``(obi_ratio, available)``.

    ``available`` is False when *payload* is missing or has zero total size
    (cannot form a real imbalance). True when at least one side has size —
    including a balanced book that correctly yields OBI≈0.
    """
    if payload is None:
        return 0.0, False
    try:
        if isinstance(payload, dict):
            model = OrderBookDepthPayload.model_validate(payload)
        else:
            model = payload
        bid_vol = float(model.total_bid_size())
        ask_vol = float(model.total_ask_size())
    except Exception:
        return 0.0, False
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0, False
    return max(-1.0, min(1.0, (bid_vol - ask_vol) / total)), True


def extract_order_book_depth(raw: Any) -> Any | None:
    """Pull L2 payload from a dict / quote / hub snapshot when present."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("order_book_depth", "depth", "order_book", "l2_depth"):
            payload = raw.get(key)
            if payload is not None:
                return payload
        return None
    for key in ("order_book_depth", "depth", "order_book", "l2_depth"):
        payload = getattr(raw, key, None)
        if payload is not None:
            return payload
    return None


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
