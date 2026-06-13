"""
Broker dealing-rule clamps — minimum stop distance for trailing modifications.
"""

from __future__ import annotations

from typing import Any

from system.pnl_math import ig_points_to_price_delta


def min_stop_distance_points(constraints: dict[str, Any] | None) -> float:
    if not constraints:
        return 1.0
    try:
        return max(1.0, float(constraints.get("min_stop_distance") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def clamp_stop_to_broker_minimum(
    side: str,
    *,
    px: float,
    stop: float,
    min_distance_points: float,
    epic: str = "",
) -> float | None:
    """
    Reject or clamp a proposed stop that violates IG minimum stop distance.

    Returns None when the stop would be too close to market (would REST-reject).
    """
    side_u = str(side or "").upper()
    min_dist = ig_points_to_price_delta(str(epic or ""), max(0.0, float(min_distance_points)))
    if min_dist <= 0:
        return float(stop)
    px_f = float(px)
    stop_f = float(stop)
    if side_u == "BUY":
        max_allowed = px_f - min_dist
        if stop_f > max_allowed:
            return None
    elif side_u == "SELL":
        min_allowed = px_f + min_dist
        if stop_f < min_allowed:
            return None
    return stop_f


def fetch_min_stop_points(rest_client: Any | None, epic: str) -> float:
    if rest_client is None or not hasattr(rest_client, "fetch_market_constraints"):
        return 1.0
    try:
        c = rest_client.fetch_market_constraints(epic)
        return min_stop_distance_points(c)
    except Exception:
        return 1.0
