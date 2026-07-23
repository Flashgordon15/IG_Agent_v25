"""Permanent IG REST budget — priority bypass must never apply to GET /positions."""

from __future__ import annotations

import time

import pytest

from system.rest_api_budget import (
    RestApiBudget,
    positions_poll_deferred,
    pressure_level_from_counts,
    priority_bypass_allowed,
)


def test_priority_bypass_denied_for_get_positions() -> None:
    assert priority_bypass_allowed("GET /positions", priority=True) is False
    assert priority_bypass_allowed("GET /positions/otc", priority=True) is False
    assert priority_bypass_allowed("GET /workingorders", priority=True) is False


def test_priority_bypass_allowed_for_confirm_and_place() -> None:
    assert priority_bypass_allowed("GET /confirms/ABC", priority=True) is True
    assert priority_bypass_allowed("POST /positions/otc", priority=True) is True
    assert priority_bypass_allowed("PUT /positions/otc/D1", priority=True) is True
    assert priority_bypass_allowed("DELETE /positions/otc", priority=True) is True
    assert priority_bypass_allowed("GET /confirms/ABC", priority=False) is False


def test_pressure_levels() -> None:
    assert pressure_level_from_counts(calls_last_minute=0, warn_per_minute=3, hard_cap=3) == "IDLE"
    assert pressure_level_from_counts(calls_last_minute=1, warn_per_minute=3, hard_cap=3) == "OK"
    assert pressure_level_from_counts(calls_last_minute=2, warn_per_minute=3, hard_cap=3) == "OK"
    assert pressure_level_from_counts(calls_last_minute=3, warn_per_minute=3, hard_cap=3) == "ELEVATED"
    assert pressure_level_from_counts(calls_last_minute=4, warn_per_minute=3, hard_cap=3) == "HIGH"
    assert pressure_level_from_counts(calls_last_minute=8, warn_per_minute=3, hard_cap=3) == "CRITICAL"


def test_get_positions_priority_does_not_bypass_interval() -> None:
    """GET /positions with priority=True must still respect min_interval (no storm)."""
    budget = RestApiBudget(min_interval_seconds=0.35, warn_per_minute=3)
    budget.set_hard_cap_per_minute(3)
    t0 = time.monotonic()
    budget.acquire(label="GET /positions/otc", priority=True)
    budget.acquire(label="GET /positions/otc", priority=True)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.30, f"expected spacing, got {elapsed:.3f}s"
    m = budget.metrics()
    assert m["calls_last_minute"] == 2
    assert "pressure_level" in m


def test_order_path_priority_still_bypasses() -> None:
    budget = RestApiBudget(min_interval_seconds=0.5, warn_per_minute=3)
    t0 = time.monotonic()
    budget.acquire(label="GET /confirms/REF1", priority=True)
    budget.acquire(label="GET /confirms/REF2", priority=True)
    elapsed = time.monotonic() - t0
    # Confirm path may bypass spacing (both under ~0.5s).
    assert elapsed < 0.45


def test_positions_poll_deferred_reads_metrics(monkeypatch) -> None:
    class _Fake:
        def metrics(self):
            return {
                "by_category_last_minute": {"positions": 9},
                "pressure_level": "HIGH",
            }

    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget", lambda: _Fake()
    )
    monkeypatch.setattr(
        "system.shared_rest_budget.over_global_limit",
        lambda *_a, **_k: False,
    )
    assert positions_poll_deferred() is True
