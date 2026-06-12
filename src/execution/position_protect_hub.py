"""
Hub-quote fast path for open-position protection — decoupled from dashboard snapshots.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from execution.trailing_stop_engine import QuoteTick

_engines: dict[str, Any] = {}
_managers: dict[str, Any] = {}
_last_hub_eval_ts: dict[str, float] = {}
_min_interval_s: float = 0.05
_stop_dispatch_ready = False


def register_execution_engine(epic: str, engine: Any) -> None:
    key = str(epic or "").strip()
    if key:
        _engines[key] = engine


def register_trade_manager(epic: str, manager: Any) -> None:
    key = str(epic or "").strip()
    if key:
        _managers[key] = manager


def get_trade_manager(epic: str) -> Any | None:
    """Lookup registered TradeManager for manual intervention paths."""
    return _managers.get(str(epic or "").strip())


def ensure_stop_dispatch_configured() -> None:
    global _stop_dispatch_ready
    if _stop_dispatch_ready:
        return
    from execution.stop_dispatch_worker import configure_stop_dispatch

    configure_stop_dispatch(_dispatch_stop_job)
    _stop_dispatch_ready = True


def _dispatch_stop_job(job: Any) -> bool:
    mgr = _managers.get(str(getattr(job, "epic", "") or ""))
    if mgr is None:
        return False
    return bool(mgr._execute_stop_dispatch_job(job))


def unregister_execution_engine(epic: str) -> None:
    key = str(epic or "").strip()
    _engines.pop(key, None)
    _managers.pop(key, None)


def reset_position_protect_hub_for_tests() -> None:
    global _stop_dispatch_ready
    _engines.clear()
    _managers.clear()
    _last_hub_eval_ts.clear()
    _stop_dispatch_ready = False


def wire_hub_quotes_to_position_protect(
    *, min_interval: float = 0.05
) -> Callable[[], None]:
    """Subscribe hub publishes → fast trailing evaluation (no GUI snapshot work)."""
    global _min_interval_s
    _min_interval_s = max(0.02, float(min_interval))

    from system.market_data_hub import on_hub_quote

    def _on_hub(snap: Any) -> None:
        epic = str(getattr(snap, "epic", "") or "").strip()
        if not epic:
            return
        engine = _engines.get(epic)
        if engine is None:
            return
        bid = float(getattr(snap, "bid", 0) or 0)
        offer = float(getattr(snap, "offer", 0) or 0)
        if bid <= 0 or offer <= 0:
            return
        now = time.time()
        if now - _last_hub_eval_ts.get(epic, 0.0) < _min_interval_s:
            return
        _last_hub_eval_ts[epic] = now
        tick = QuoteTick(
            bid=bid,
            offer=offer,
            epic=epic,
            market="",
            ts=now,
        )
        try:
            engine.update_positions_fast(tick)
        except Exception:
            pass

    return on_hub_quote(_on_hub)
