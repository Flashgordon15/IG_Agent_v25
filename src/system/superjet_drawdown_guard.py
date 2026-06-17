"""
Superjet hard daily drawdown ceiling — emergency flatten + freeze until midnight UK.

Additive safety layer (does not replace max_daily_loss_gbp in capital envelope).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

MAX_DAILY_DRAWDOWN_GBP = 500.0
_LONDON = ZoneInfo("Europe/London")

_lock = threading.Lock()
_frozen = False
_frozen_at: float = 0.0
_last_pnl: float = 0.0
_enforce_inflight = False


def is_frozen() -> bool:
    global _frozen
    with _lock:
        if not _frozen:
            return False
        if _past_midnight_reset():
            _frozen = False
            _frozen_at = 0.0
            return False
        return True


def _past_midnight_reset() -> bool:
    if _frozen_at <= 0:
        return False
    frozen_day = datetime.fromtimestamp(_frozen_at, tz=_LONDON).date()
    today = datetime.now(_LONDON).date()
    return today > frozen_day


def reset_superjet_drawdown_guard_for_tests() -> None:
    global _frozen, _frozen_at, _last_pnl, _enforce_inflight
    with _lock:
        _frozen = False
        _frozen_at = 0.0
        _last_pnl = 0.0
        _enforce_inflight = False


def _resolve_daily_pnl_gbp() -> float:
    try:
        from intelligence.target_engine import get_target_engine, target_engine_enabled
        from system.config_loader import get_config

        if target_engine_enabled(get_config(reload=False)):
            snap = get_target_engine().refresh()
            realized = float(snap.get("realized_daily_pnl_gbp") or 0.0)
            unrealized = float(snap.get("open_unrealized_gbp") or 0.0)
            return realized + unrealized
    except Exception:
        pass
    try:
        from trading.points_engine import get_points_engine

        pe = get_points_engine()
        if pe is not None:
            return float(pe.effective_daily_loss_gbp() or 0.0) * -1.0
    except Exception:
        pass
    return 0.0


def telemetry_snapshot() -> dict[str, Any]:
    pnl = _resolve_daily_pnl_gbp()
    with _lock:
        global _last_pnl
        _last_pnl = pnl
        return {
            "daily_pnl_gbp": round(pnl, 2),
            "ceiling_gbp": -MAX_DAILY_DRAWDOWN_GBP,
            "frozen": _frozen and not _past_midnight_reset(),
            "breached": pnl <= -MAX_DAILY_DRAWDOWN_GBP,
        }


def check_and_enforce_async() -> dict[str, Any]:
    """
    Called from cockpit collector thread — spawns emergency worker if breached.
    Returns current guard snapshot.
    """
    snap = telemetry_snapshot()
    if not snap.get("breached") or is_frozen():
        return snap

    global _enforce_inflight
    with _lock:
        if _enforce_inflight:
            return snap
        _enforce_inflight = True

    def _worker() -> None:
        global _frozen, _frozen_at, _enforce_inflight
        try:
            _engage_freeze()
            log_engine(
                f"SUPERJET DRAWDOWN CEILING: daily P&L {snap['daily_pnl_gbp']:.2f} GBP "
                f"<= -{MAX_DAILY_DRAWDOWN_GBP:.2f} — emergency flatten engaged"
            )
            from system.supervisor_history import record_supervisor_event

            record_supervisor_event(
                "drawdown_ceiling_breach",
                detail=f"P&L {snap['daily_pnl_gbp']:.2f} GBP",
                payload=snap,
                source="superjet_drawdown_guard",
            )
            try:
                from cockpit.emergency import execute_emergency_cockpit_override

                execute_emergency_cockpit_override()
            except Exception as exc:
                log_engine(
                    f"SUPERJET flatten failed: {type(exc).__name__}: {exc}"
                )
            try:
                from system.shutdown_cleanup import mark_manual_stop

                mark_manual_stop(source="superjet_drawdown_ceiling")
            except Exception:
                pass
        finally:
            with _lock:
                _enforce_inflight = False

    threading.Thread(target=_worker, name="SuperjetDrawdownEnforce", daemon=True).start()
    return snap


def _engage_freeze() -> None:
    global _frozen, _frozen_at
    with _lock:
        _frozen = True
        _frozen_at = time.time()
