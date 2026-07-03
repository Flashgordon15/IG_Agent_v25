"""Tests for instant health lane and API health grace."""

from __future__ import annotations

import time

from api.health_instant import build_instant_health_response
from api.health_light import get_health_light_response, start_health_light_refresher
from system.boot.api_health_grace import (
    health_grace_active,
    mark_api_bound,
    reset_api_health_grace_for_tests,
)


def test_health_light_read_does_not_block_during_swap():
    start_health_light_refresher()
    a = get_health_light_response()
    b = get_health_light_response()
    assert isinstance(a, dict)
    assert isinstance(b, dict)


def test_instant_health_response_is_lightweight():
    reset_api_health_grace_for_tests()
    mark_api_bound()
    body = build_instant_health_response()
    assert body.get("ok") is True
    assert body.get("instant_lane") is True
    assert "health_light" in body


def test_health_grace_window():
    reset_api_health_grace_for_tests()
    assert health_grace_active() is True
    mark_api_bound()
    assert health_grace_active() is True
    time.sleep(0.01)
    assert health_grace_active() is True
