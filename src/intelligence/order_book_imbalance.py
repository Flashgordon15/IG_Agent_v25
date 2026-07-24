"""
Order Book Imbalance (OBI) — high-speed ratio from Level-2 depth payloads.

When true L2 is unavailable (Mini ``rest_poll`` / Yahoo / IG quote path),
callers may use ``compute_proxy_obi_from_mids`` — a **quote-proxy** imbalance
from rolling mid returns, **not** institutional book depth.
"""

from __future__ import annotations

from typing import Any, Sequence

from cockpit.telemetry_schema import OrderBookDepthPayload


def compute_proxy_obi_from_mids(
    mids: Sequence[float],
    spread: float,
    *,
    min_points: int = 2,
) -> tuple[float, bool]:
    """
    Depth-free OBI proxy in ``[-1, 1]`` from a rolling mid series.

    This is **not** true L2 order-book imbalance. It estimates signed pressure
    from mid returns normalized by recent up/down move mass so a trending
    market yields a non-zero reading.

    Returns ``(ratio, available)``. ``available`` is False when history is
    missing/short or the series is flat (no informative move) — callers must
    fail-closed rather than treat 0.0 as a balanced book.
    """
    try:
        vals = [float(m) for m in mids if m is not None and float(m) > 0.0]
    except (TypeError, ValueError):
        return 0.0, False
    if len(vals) < max(2, int(min_points)):
        return 0.0, False
    try:
        sp = abs(float(spread))
    except (TypeError, ValueError):
        sp = 0.0
    mid_range = max(vals) - min(vals)
    # Flat rest_poll republishes → no proxy signal (fail-closed).
    min_move = max(sp * 0.25, abs(vals[-1]) * 1e-8, 1e-9)
    if mid_range < min_move:
        return 0.0, False
    diffs = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    up = sum(d for d in diffs if d > 0.0)
    down = sum(-d for d in diffs if d < 0.0)
    total = up + down
    if total <= 0.0:
        return 0.0, False
    ratio = (up - down) / total
    return max(-1.0, min(1.0, float(ratio))), True


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
