"""
Live broker order router — v31.1 dynamic stop-distance discovery + step-trailing.

Floors illegal trailing / placement stops to IG market-metadata minimums so
``STOPS_NEAREST_ALLOWED_EXCEEDED`` rejections are suppressed before REST dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execution.dealing_constraints import fetch_min_stop_points
from system.engine_log import log_engine
from system.pnl_math import ig_points_to_price_delta, pip_size_for_epic

STOPS_NEAREST_REJECT = "STOPS_NEAREST_ALLOWED_EXCEEDED"


@dataclass(frozen=True)
class StopDistanceResolution:
    """Broker-minimum stop metadata for an epic."""

    epic: str
    requested_points: float
    min_points: float
    effective_points: float
    source: str = "dealingRules.minNormalStopOrLimitDistance"


def resolve_min_stop_distance_points(rest_client: Any | None, epic: str) -> float:
    """Discover minimum allowed stop distance (IG points) from market metadata."""
    return max(1.0, float(fetch_min_stop_points(rest_client, epic)))


def floor_stop_distance_points(
    rest_client: Any | None,
    epic: str,
    requested_points: float,
) -> StopDistanceResolution:
    """
    Floor a requested stop distance to the broker minimum (e.g. 2 → 12 points).
    """
    min_pts = resolve_min_stop_distance_points(rest_client, epic)
    req = float(requested_points)
    effective = max(req, min_pts)
    if effective > req + 1e-9:
        log_engine(
            f"LiveBrokerOrderRouter: floored stop distance epic={epic} "
            f"{req:g}→{effective:g} pts (broker min)"
        )
    return StopDistanceResolution(
        epic=str(epic),
        requested_points=req,
        min_points=min_pts,
        effective_points=effective,
    )


def floor_stop_level_to_broker_minimum(
    side: str,
    *,
    market_price: float,
    proposed_stop: float,
    min_distance_points: float,
    epic: str = "",
) -> float:
    """
    Shift an absolute stop level to the nearest legal distance from market.

    Unlike ``clamp_stop_to_broker_minimum`` (returns None), this always returns
    a broker-legal level — suppressing STOPS_NEAREST_ALLOWED_EXCEEDED.
    """
    side_u = str(side or "").upper()
    px = float(market_price)
    stop = float(proposed_stop)
    min_dist = ig_points_to_price_delta(str(epic or ""), max(0.0, float(min_distance_points)))
    if min_dist <= 0 or px <= 0:
        return stop
    if side_u == "BUY":
        # Buy stop sits below market — cannot be closer than min_dist.
        legal_ceiling = px - min_dist
        if stop > legal_ceiling:
            return legal_ceiling
    elif side_u == "SELL":
        legal_floor = px + min_dist
        if stop < legal_floor:
            return legal_floor
    return stop


def normalize_placement_distances(
    rest_client: Any | None,
    epic: str,
    *,
    stop_distance: float,
    limit_distance: float | None = None,
) -> tuple[float, float | None, StopDistanceResolution]:
    """Normalize MARKET entry stop/limit distances before POST."""
    res = floor_stop_distance_points(rest_client, epic, stop_distance)
    stop = res.effective_points
    limit: float | None
    if limit_distance is not None and float(limit_distance) > 0:
        limit = max(float(limit_distance), stop)
    else:
        limit = None
    return stop, limit, res


@dataclass(frozen=True)
class StepTrailUpdate:
    """Absolute stop/limit levels for a trailing PUT."""

    epic: str
    direction: str
    deal_id: str
    stop_level: float
    limit_level: float
    step_points: float
    min_stop_points: float
    floored_step: bool
    market_price: float


def compute_step_trail_update(
    rest_client: Any | None,
    *,
    epic: str,
    direction: str,
    deal_id: str,
    entry_level: float,
    step_points: float,
    scalp_limit_points: float,
    iteration: int = 0,
    market_price: float | None = None,
) -> StepTrailUpdate:
    """
    Compute step-trailing stop/limit levels with broker-minimum flooring.

    Step distance is raised to ``min_stop_points`` when the broker requires a
  wider trail (e.g. 2pt request → 12pt legal minimum).
    """
    min_pts = resolve_min_stop_distance_points(rest_client, epic)
    req_step = float(step_points)
    effective_step = max(req_step, min_pts)
    floored = effective_step > req_step + 1e-9
    if floored:
        log_engine(
            f"LiveBrokerOrderRouter: step-trail floored epic={epic} "
            f"{req_step:g}→{effective_step:g} pts"
        )

    px = float(market_price if market_price is not None else entry_level)
    level = float(entry_level)
    direction_u = str(direction or "").upper()

    # Progressive step — tighten trail each iteration without violating min distance.
    trail_offset_pts = effective_step * (1.0 + 0.1 * max(0, int(iteration)))

    if pip_size_for_epic(epic):
        delta = trail_offset_pts * float(pip_size_for_epic(epic) or 0.0001)
        limit_delta = max(float(scalp_limit_points), min_pts) * float(
            pip_size_for_epic(epic) or 0.0001
        )
    else:
        delta = ig_points_to_price_delta(epic, trail_offset_pts)
        limit_delta = ig_points_to_price_delta(epic, max(float(scalp_limit_points), min_pts))

    if direction_u == "BUY":
        raw_stop = level - delta
        raw_limit = level + limit_delta
    else:
        raw_stop = level + delta
        raw_limit = level - limit_delta

    stop_level = floor_stop_level_to_broker_minimum(
        direction_u,
        market_price=px,
        proposed_stop=raw_stop,
        min_distance_points=min_pts,
        epic=epic,
    )
    limit_level = floor_stop_level_to_broker_minimum(
        "SELL" if direction_u == "BUY" else "BUY",
        market_price=px,
        proposed_stop=raw_limit,
        min_distance_points=min_pts,
        epic=epic,
    )

    return StepTrailUpdate(
        epic=str(epic),
        direction=direction_u,
        deal_id=str(deal_id),
        stop_level=stop_level,
        limit_level=limit_level,
        step_points=trail_offset_pts,
        min_stop_points=min_pts,
        floored_step=floored,
        market_price=px,
    )


def apply_step_trail_put(
    rest_client: Any,
    update: StepTrailUpdate,
    *,
    budget_priority: bool = True,
) -> dict[str, Any]:
    """Dispatch trailing stop PUT with priority budget bypass."""
    return rest_client.update_position_stops(
        update.deal_id,
        stop_level=update.stop_level,
        limit_level=update.limit_level,
        budget_priority=budget_priority,
    )
