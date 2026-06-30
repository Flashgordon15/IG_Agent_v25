"""Stubborn IG execution guard — pause orders under rate limit, allow signals."""

from __future__ import annotations

from system.engine_log import log_engine

_last_pause_log_ts = 0.0


def ig_execution_allowed() -> tuple[bool, str]:
    """
    Returns (allowed, reason).

    When rate-limited: block order submission only; signals/lifecycle continue.
    """
    try:
        from system.ig_budget_monitor import execution_paused, ig_budget_snapshot

        if execution_paused():
            snap = ig_budget_snapshot()
            remaining = int(snap.get("cooldown_seconds_remaining") or 0)
            return False, f"ig_rate_limited:{remaining}s"
    except Exception:
        pass
    return True, ""


def log_execution_paused_if_needed(reason: str) -> None:
    global _last_pause_log_ts
    import time

    now = time.time()
    if now - _last_pause_log_ts < 30.0:
        return
    _last_pause_log_ts = now
    log_engine(f"IG execution guard: paused — {reason} (signals continue)")
