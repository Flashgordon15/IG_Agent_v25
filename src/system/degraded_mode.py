"""
Degraded mode — when IG REST is rate-limited, block non-essential API calls.

Keeps UI honest (no silent fallbacks to local SIM history) while protecting quota.
"""

from __future__ import annotations

from system.rate_limit_manager import get_rate_limit_manager

_ESSENTIAL = frozenset({"position_sync", "positions", "orders"})


def is_degraded() -> bool:
    from system.rest_api_budget import non_essential_rest_paused

    return get_rate_limit_manager().is_rest_blocked() or non_essential_rest_paused()


def allow_rest_operation(operation: str) -> bool:
    """Return False when REST is paused and operation is non-essential."""
    from system.rest_api_budget import non_essential_rest_paused, order_in_flight_paused

    if order_in_flight_paused(operation):
        return False
    if non_essential_rest_paused():
        return operation in _ESSENTIAL
    if get_rate_limit_manager().is_rest_blocked():
        return operation in _ESSENTIAL
    return True


def degraded_user_message() -> str:
    mgr = get_rate_limit_manager()
    if not mgr.is_active():
        return ""
    return f"IG REST paused — retry in {mgr.format_countdown()}"
