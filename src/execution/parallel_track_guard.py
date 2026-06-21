"""
Process-isolated parallel track guard — fail-closed live broker transmission boundary.

Only the Live Vanguard track (``IG_PARALLEL_TRACK=live``) may reach
``LiveExecutor._execute_order_blocking`` / IG REST ``place_market_order``.
"""

from __future__ import annotations

from system.guard.security_errors import FailClosedSecurityError
from system.identity.shared_memory_bridge import resolve_parallel_track_key


def enforce_live_track_or_fail(*, epic: str = "") -> None:
    """Fatal fail-closed gate — shadow track must never reach IG REST."""
    track = resolve_parallel_track_key()
    if track == "shadow":
        suffix = f" epic={epic}" if epic else ""
        raise FailClosedSecurityError(
            "ParallelTrackGuard: shadow track is mock-replay only — "
            f"live broker transmission blocked before network I/O{suffix}"
        )
    if track != "live":
        raise FailClosedSecurityError(
            f"ParallelTrackGuard: unknown track={track!r} — transmission blocked"
        )


def assert_live_track_order_transmission(*, epic: str = "") -> tuple[bool, str]:
    """
    Final parallel-architecture gate before live REST order transmission.

    Returns ``(True, "ok")`` when the current process is permitted to transmit.
    """
    track = resolve_parallel_track_key()
    if track == "shadow":
        suffix = f" epic={epic}" if epic else ""
        return (
            False,
            "ParallelTrackGuard: shadow track is mock-replay only — "
            f"live broker transmission blocked{suffix}",
        )
    if track != "live":
        return False, f"ParallelTrackGuard: unknown track={track!r} — transmission blocked"
    return True, "ok"
