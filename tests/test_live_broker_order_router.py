"""Unit tests for v31.1 live broker order router stop-distance flooring."""

from __future__ import annotations

from execution.live_broker_order_router import (
    floor_stop_distance_points,
    floor_stop_level_to_broker_minimum,
    normalize_placement_distances,
)


class _FakeRest:
    def fetch_market_constraints(self, epic: str) -> dict:
        return {"min_stop_distance": 12.0}


def test_floor_stop_distance_points_raises_to_broker_min():
    res = floor_stop_distance_points(_FakeRest(), "IX.D.DOW.IFM.IP", 2.0)
    assert res.requested_points == 2.0
    assert res.min_points == 12.0
    assert res.effective_points == 12.0


def test_normalize_placement_distances_floors_limit():
    stop, limit, res = normalize_placement_distances(
        _FakeRest(),
        "IX.D.DOW.IFM.IP",
        stop_distance=2.0,
        limit_distance=5.0,
    )
    assert stop == 12.0
    assert limit == 12.0
    assert res.effective_points == 12.0


def test_floor_stop_level_buy_shifts_outward():
    # BUY stop below market — 44000 with 12pt min on index (~12 points = 12.0 price units for index?)
    # For DOW, pip_size is None so ig_points_to_price_delta uses points as price delta
    level = floor_stop_level_to_broker_minimum(
        "BUY",
        market_price=44000.0,
        proposed_stop=43995.0,
        min_distance_points=12.0,
        epic="IX.D.DOW.IFM.IP",
    )
    assert level <= 43988.0  # at least 12 away from 44000
