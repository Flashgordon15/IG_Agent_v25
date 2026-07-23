"""Feed-health watchdog — stale quote blocks entries; healthy clears."""

from __future__ import annotations

from runtime.feed_health_watchdog import (
    QUOTE_STALE_SEC,
    entries_blocked_by_feed_health,
    is_system_healthy,
    reset_feed_health_watchdog_for_tests,
    system_health_snapshot,
    _mark_healthy,
    _mark_unhealthy,
)


def setup_function() -> None:
    reset_feed_health_watchdog_for_tests()


def teardown_function() -> None:
    reset_feed_health_watchdog_for_tests()


def test_stale_quote_marks_unhealthy_and_blocks_entries() -> None:
    _mark_unhealthy(QUOTE_STALE_SEC + 1.0, "unit_stale")
    assert is_system_healthy() is False
    assert entries_blocked_by_feed_health() is True
    snap = system_health_snapshot()
    assert snap["is_healthy"] is False
    assert snap["entries_blocked"] is True
    assert snap["operational_badge"] is False


def test_fresh_quote_marks_operational(monkeypatch) -> None:
    monkeypatch.setattr(
        "runtime.feed_health_watchdog._transport_quote_budget_sec",
        lambda: 0.5,
    )
    _mark_healthy(0.2)
    assert is_system_healthy() is True
    assert entries_blocked_by_feed_health() is False
    snap = system_health_snapshot()
    assert snap["operational_badge"] is True
    assert snap["quote_age_ms"] == 200.0


def test_rest_poll_budget_allows_operational_badge(monkeypatch) -> None:
    monkeypatch.setattr(
        "runtime.feed_health_watchdog._transport_quote_budget_sec",
        lambda: 10.0,
    )
    _mark_healthy(4.5)
    snap = system_health_snapshot()
    assert snap["is_healthy"] is True
    assert snap["operational_badge"] is True


def test_resolve_quote_age_ignores_poisoned_memory_context(monkeypatch) -> None:
    """Unhealthy marks must not permanently lock age at 999 via memory_context."""
    from runtime import feed_health_watchdog as fhw

    class _Mem:
        def quote_age_sec(self) -> float:
            return 999.0

    monkeypatch.setattr(
        "system.memory_context.get_memory_context",
        lambda: _Mem(),
    )

    class _Snap:
        def age_seconds(self) -> float:
            return 0.3

    class _Hub:
        def get_snapshot(self, epic: str):
            return _Snap()

    monkeypatch.setattr(
        "system.market_data_hub.get_market_data_hub",
        lambda: _Hub(),
    )
    age = fhw._resolve_quote_age_sec()
    assert age == 0.3


def test_rest_coalesce_error_helper() -> None:
    from runtime.feed_health_watchdog import _is_rest_coalesce_error

    assert _is_rest_coalesce_error("REST deferred (positions_coalesce_pressure)")
    assert not _is_rest_coalesce_error("connection reset")


def test_entries_blocked_clears_when_hub_fresh(monkeypatch) -> None:
    from runtime import feed_health_watchdog as fhw

    _mark_unhealthy(30.0, "unit_sticky")
    assert entries_blocked_by_feed_health() is True

    class _Snap:
        def age_seconds(self) -> float:
            return 0.2

    class _Hub:
        def get_snapshot(self, epic: str):
            return _Snap()

    monkeypatch.setattr(
        "system.market_data_hub.get_market_data_hub",
        lambda: _Hub(),
    )
    monkeypatch.setattr(
        fhw,
        "_resolve_quote_age_sec",
        lambda: 0.2,
    )
    assert entries_blocked_by_feed_health() is False
    assert is_system_healthy() is True
