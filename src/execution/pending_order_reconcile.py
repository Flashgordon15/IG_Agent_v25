"""
Pending broker order reconciliation — handle uncertain submit/confirm responses.

When an entry or exit response is delayed, transport-errored, or returns no
clear accepted/rejected verdict, the order is marked "pending confirmation".
Subsequent orders for the same epic are blocked until either:

- broker reconciliation observes a matching position state, or
- an explicit resolve call clears the pending entry, or
- the configured unresolved warning threshold is exceeded (still blocked,
  but a throttled warning is emitted).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine


def _request_save() -> None:
    try:
        from system.runtime_state_persist import request_save

        request_save()
    except Exception:
        pass


DEFAULT_PENDING_TIMEOUT_SEC = 30.0
_UNRESOLVED_LOG_INTERVAL_SEC = 60.0
# Ghost-order defence: auto-expire pending entries that are older than this,
# regardless of whether reconciliation was ever called.  Set to 5 minutes —
# long enough to survive a slow broker confirm cycle, short enough that a
# rate-cap-deferred order with ref="-" can never block a market indefinitely.
PENDING_HARD_EXPIRY_SEC = 300.0
# On startup, skip loading any pending entry that is older than this (seconds).
# Orders from a previous session are stale; broker reconciliation will rebuild
# the correct state within seconds of the first position-sync tick.
_PENDING_LOAD_MAX_AGE_SEC = 120.0

ORDER_TYPE_ENTRY = "entry"
ORDER_TYPE_EXIT = "exit"

_lock = threading.RLock()
_pending: dict[str, "PendingOrder"] = {}
_last_unresolved_log_ts: dict[str, float] = {}


@dataclass(frozen=True)
class PendingOrder:
    epic: str
    side: str
    order_type: str
    local_created_at: float
    broker_deal_reference: str = ""


def _epic_key(epic: str) -> str:
    return str(epic or "").strip()


def mark_pending(
    epic: str,
    *,
    side: str,
    order_type: str,
    deal_reference: str = "",
) -> None:
    """Mark an order as pending broker confirmation."""
    key = _epic_key(epic)
    if not key:
        return
    if order_type not in (ORDER_TYPE_ENTRY, ORDER_TYPE_EXIT):
        return
    now = time.time()
    with _lock:
        _pending[key] = PendingOrder(
            epic=key,
            side=str(side or "").upper(),
            order_type=order_type,
            local_created_at=now,
            broker_deal_reference=str(deal_reference or "").strip(),
        )
    log_engine(
        f"Order confirmation pending for {key} ({order_type} {side or '?'} "
        f"ref={deal_reference or '-'})"
    )
    _request_save()


def set_pending_deal_reference(epic: str, deal_reference: str) -> None:
    key = _epic_key(epic)
    if not key:
        return
    with _lock:
        rec = _pending.get(key)
        if rec is None:
            return
        _pending[key] = PendingOrder(
            epic=rec.epic,
            side=rec.side,
            order_type=rec.order_type,
            local_created_at=rec.local_created_at,
            broker_deal_reference=str(deal_reference or "").strip(),
        )
    _request_save()


def has_pending(epic: str, *, expiry_sec: float = PENDING_HARD_EXPIRY_SEC) -> bool:
    """Return True only if there is a live (non-expired) pending order for this epic.

    Entries older than *expiry_sec* are silently removed so a rate-cap-deferred
    order with ref="-" (that never reached IG) can never permanently block a
    market.  The 5-minute default is far longer than any legitimate broker
    confirm cycle, so genuine uncertain orders are not cleared prematurely.
    """
    key = _epic_key(epic)
    if not key:
        return False
    now = time.time()
    with _lock:
        rec = _pending.get(key)
        if rec is None:
            return False
        age = now - rec.local_created_at
        if age > max(1.0, float(expiry_sec)):
            _pending.pop(key, None)
            _last_unresolved_log_ts.pop(key, None)
            log_engine(
                f"Pending order for {key} auto-expired after {age:.0f}s "
                f"(ref={rec.broker_deal_reference or '-'}) — cleared"
            )
            _request_save()
            return False
        return True


def get_pending(epic: str) -> PendingOrder | None:
    key = _epic_key(epic)
    if not key:
        return None
    with _lock:
        return _pending.get(key)


def resolve_pending(epic: str, *, reason: str = "") -> bool:
    """Clear pending state for an epic. Returns True if there was state to clear."""
    key = _epic_key(epic)
    if not key:
        return False
    with _lock:
        rec = _pending.pop(key, None)
        _last_unresolved_log_ts.pop(key, None)
    if rec is not None and reason:
        log_engine(
            f"Order confirmation resolved for {key} ({rec.order_type}) — {reason}"
        )
    if rec is not None:
        _request_save()
    return rec is not None


def is_unresolved_overdue(
    epic: str, *, timeout_sec: float = DEFAULT_PENDING_TIMEOUT_SEC
) -> bool:
    rec = get_pending(epic)
    if rec is None:
        return False
    return (time.time() - rec.local_created_at) > max(1.0, float(timeout_sec))


def log_unresolved_if_due(
    epic: str, *, timeout_sec: float = DEFAULT_PENDING_TIMEOUT_SEC
) -> None:
    """Throttled warning when pending exceeds timeout. Does not clear state."""
    if not is_unresolved_overdue(epic, timeout_sec=timeout_sec):
        return
    key = _epic_key(epic)
    now = time.time()
    with _lock:
        last = _last_unresolved_log_ts.get(key, 0.0)
        if now - last < _UNRESOLVED_LOG_INTERVAL_SEC:
            return
        _last_unresolved_log_ts[key] = now
    log_engine(
        f"Order confirmation unresolved for {key} — trading paused until reconciliation"
    )
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(f"{key} order confirmation unresolved")
    except Exception as e:
        log_engine(f"telegram unresolved-order alert failed: {type(e).__name__}: {e}")


def reconcile_pending_via_position_state(epic: str, *, position_present: bool) -> None:
    """Clear pending entry when broker shows a position; clear pending exit when absent."""
    rec = get_pending(epic)
    if rec is None:
        return
    if rec.order_type == ORDER_TYPE_ENTRY and position_present:
        resolve_pending(epic, reason="entry confirmed by broker reconciliation")
    elif rec.order_type == ORDER_TYPE_EXIT and not position_present:
        resolve_pending(epic, reason="exit confirmed by broker reconciliation")


def recover_pending_state_for_startup() -> int:
    """Wipe pending tracker on bot startup — reconciliation will rebuild as needed."""
    with _lock:
        cleared = len(_pending)
        _pending.clear()
        _last_unresolved_log_ts.clear()
    return cleared


def reset_pending_state_for_tests() -> None:
    with _lock:
        _pending.clear()
        _last_unresolved_log_ts.clear()


def dump_pending_state() -> dict[str, Any]:
    with _lock:
        return {
            "orders": [
                {
                    "epic": p.epic,
                    "side": p.side,
                    "order_type": p.order_type,
                    "local_created_at": p.local_created_at,
                    "broker_deal_reference": p.broker_deal_reference,
                }
                for p in _pending.values()
            ]
        }


def load_pending_state(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    items = data.get("orders") or []
    if not isinstance(items, list):
        return
    now = time.time()
    with _lock:
        _pending.clear()
        for item in items:
            if not isinstance(item, dict):
                continue
            epic = _epic_key(str(item.get("epic") or ""))
            if not epic:
                continue
            order_type = str(item.get("order_type") or "")
            if order_type not in (ORDER_TYPE_ENTRY, ORDER_TYPE_EXIT):
                continue
            try:
                ts = float(item.get("local_created_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            # Skip stale entries from previous sessions — broker reconciliation
            # rebuilds accurate state within seconds of the first position-sync tick.
            if (now - ts) > _PENDING_LOAD_MAX_AGE_SEC:
                log_engine(
                    f"pending_order_reconcile: skipped stale pending for {epic} "
                    f"(age={(now - ts):.0f}s > {_PENDING_LOAD_MAX_AGE_SEC:.0f}s limit)"
                )
                continue
            _pending[epic] = PendingOrder(
                epic=epic,
                side=str(item.get("side") or "").upper(),
                order_type=order_type,
                local_created_at=ts,
                broker_deal_reference=str(item.get("broker_deal_reference") or ""),
            )
