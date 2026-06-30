"""
IG rate-budget monitor — rolling 30m REST accounting + demo quota estimation.

Combines RestApiBudget call history with RateLimitManager cooldown state.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

_WINDOW_SEC = 30 * 60
# Conservative IG demo allowance estimate (orders + confirms + sync per 30m).
_DEMO_BUDGET_30M = 48
_EXEC_CATEGORIES = frozenset({"orders", "positions"})
_lock = threading.RLock()
_execution_paused_logged = False


def _utc_iso(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def record_rest_call(label: str, category: str) -> None:
    """Hook after successful REST acquire (optional — budget deque already tracks)."""
    _ = (label, category)


def calls_last_30m() -> int:
    from system.rest_api_budget import get_rest_api_budget

    return get_rest_api_budget().calls_in_window(_WINDOW_SEC)


def calls_by_endpoint_30m() -> dict[str, int]:
    from system.rest_api_budget import get_rest_api_budget

    return get_rest_api_budget().endpoint_counts_in_window(_WINDOW_SEC)


def execution_calls_last_30m() -> int:
    from system.rest_api_budget import get_rest_api_budget

    return get_rest_api_budget().calls_in_window(
        _WINDOW_SEC, categories=_EXEC_CATEGORIES
    )


def is_rate_limited() -> bool:
    from system.rate_limit_manager import get_rate_limit_manager

    return get_rate_limit_manager().is_rest_blocked()


def cooldown_until_ts() -> float:
    from system.rate_limit_manager import get_rate_limit_manager

    snap = get_rate_limit_manager().snapshot()
    if not snap.active:
        return 0.0
    return time.time() + max(0.0, snap.rest_seconds_remaining)


def estimated_budget_remaining() -> int:
    used = execution_calls_last_30m()
    return max(0, _DEMO_BUDGET_30M - used)


def execution_paused() -> bool:
    """Stubborn guard — pause order submission when IG quota cooldown active."""
    return is_rate_limited()


def ig_budget_snapshot() -> dict[str, Any]:
    from system.rate_limit_manager import get_rate_limit_manager
    from system.rest_api_budget import get_rest_api_budget

    rl = get_rate_limit_manager().snapshot()
    budget = get_rest_api_budget().metrics()
    limited = is_rate_limited()
    cooldown_ts = cooldown_until_ts()
    global _execution_paused_logged
    if limited and not _execution_paused_logged:
        _execution_paused_logged = True
        try:
            from system.engine_log import log_engine
            from system.unified_runtime_state import emit_event

            log_engine("IG rate-limited — execution paused, analysis continues")
            emit_event(
                "ig_rate_limited",
                {"cooldown_until": _utc_iso(cooldown_ts)},
            )
        except Exception:
            pass
    if not limited:
        _execution_paused_logged = False

    return {
        "ok": True,
        "calls_last_30m": calls_last_30m(),
        "execution_calls_last_30m": execution_calls_last_30m(),
        "calls_by_endpoint_30m": calls_by_endpoint_30m(),
        "calls_last_minute": budget.get("calls_last_minute", 0),
        "estimated_budget_remaining": estimated_budget_remaining(),
        "estimated_budget_30m": _DEMO_BUDGET_30M,
        "rate_limited": limited,
        "execution_paused": execution_paused(),
        "cooldown_until": _utc_iso(cooldown_ts),
        "cooldown_seconds_remaining": int(max(0.0, rl.rest_seconds_remaining)),
        "backoff_stage": rl.backoff_stage,
        "blocked_calls": rl.blocked_calls,
        "rest_budget": budget,
    }


def reset_ig_budget_monitor_for_tests() -> None:
    global _execution_paused_logged
    with _lock:
        _execution_paused_logged = False
