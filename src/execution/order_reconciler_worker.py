"""
Background scavenger for ambiguous broker orders (PENDING_RECONCILE).

Polls every 30s, queries IG for deal/position truth, and releases gate
reservations when the broker confirms failure.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from execution.pending_order_reconcile import (
    DEFAULT_PENDING_TIMEOUT_SEC,
    ORDER_TYPE_ENTRY,
    PENDING_RECONCILE,
    PendingOrder,
    get_pending,
    list_pending_orders,
    resolve_pending,
)
from system.engine_log import log_engine
from system.sync_task_guard import RECONCILE_TASK_GUARD

RECONCILE_INTERVAL_SEC = 30.0

_worker_ref: OrderReconcilerWorker | None = None


def _broker_position_present(rest_client: Any, epic: str, side: str) -> bool:
    if rest_client is None:
        return False
    try:
        if hasattr(rest_client, "has_open_position"):
            if rest_client.has_open_position(epic):
                return True
    except Exception:
        pass
    try:
        want_side = str(side or "").upper()
        for item in rest_client.open_positions():
            mkt = item.get("market") or {}
            pos = item.get("position") or {}
            if str(mkt.get("epic") or "") != epic:
                continue
            if want_side and str(pos.get("direction") or "").upper() != want_side:
                continue
            return True
    except Exception:
        pass
    return False


def _confirm_snapshot(rest_client: Any, deal_reference: str) -> dict[str, Any] | None:
    if not deal_reference or rest_client is None:
        return None
    if not hasattr(rest_client, "confirm_deal"):
        return None
    try:
        return rest_client.confirm_deal(
            deal_reference,
            max_wait_seconds=1.5,
            poll_interval_seconds=0.5,
        )
    except Exception as e:
        log_engine(
            f"OrderReconcilerWorker confirm_deal failed ref={deal_reference}: "
            f"{type(e).__name__}: {e}"
        )
        return None


def _position_by_deal(rest_client: Any, deal_id: str) -> dict[str, Any] | None:
    if not deal_id or rest_client is None:
        return None
    if hasattr(rest_client, "get_position_otc"):
        try:
            return rest_client.get_position_otc(deal_id)
        except Exception:
            pass
    if hasattr(rest_client, "find_open_position"):
        try:
            return rest_client.find_open_position(deal_id)
        except Exception:
            pass
    return None


def reconcile_pending_order(
    rest_client: Any,
    pending: PendingOrder,
    *,
    config: Any | None = None,
    stale_entry_grace_sec: float = DEFAULT_PENDING_TIMEOUT_SEC,
) -> bool:
    """
    Verify one pending order against IG. Returns True if pending state was cleared.
    """
    epic = pending.epic
    age = time.time() - pending.local_created_at
    ref = pending.broker_deal_reference

    if pending.order_type == ORDER_TYPE_ENTRY:
        confirm = _confirm_snapshot(rest_client, ref) if ref else None
        if confirm:
            if confirm.get("accepted"):
                deal_id = str(confirm.get("deal_id") or "")
                if deal_id and _position_by_deal(rest_client, deal_id):
                    resolve_pending(
                        epic, reason="entry confirmed by OrderReconcilerWorker"
                    )
                    return True
                if _broker_position_present(rest_client, epic, pending.side):
                    resolve_pending(
                        epic, reason="entry position visible — reconciler cleared"
                    )
                    return True
            if confirm.get("rejected"):
                try:
                    from execution.portfolio_hooks import (
                        release_stashed_pending_portfolio,
                    )

                    release_stashed_pending_portfolio(epic, config=config)
                except Exception as e:
                    log_engine(
                        f"OrderReconcilerWorker portfolio release failed epic={epic}: "
                        f"{type(e).__name__}: {e}"
                    )
                resolve_pending(
                    epic,
                    reason=f"entry rejected by broker ({confirm.get('reason') or 'rejected'})",
                )
                return True

        deal_id = str((confirm or {}).get("deal_id") or "")
        if deal_id:
            if _position_by_deal(rest_client, deal_id):
                resolve_pending(
                    epic, reason="OTC position verified by dealId"
                )
                return True

        if _broker_position_present(rest_client, epic, pending.side):
            resolve_pending(
                epic, reason="open position matched epic during reconcile"
            )
            return True

        if pending.pending_reconcile and age >= float(stale_entry_grace_sec):
            try:
                from execution.portfolio_hooks import (
                    release_stashed_pending_portfolio,
                )

                released = release_stashed_pending_portfolio(epic, config=config)
                if released > 0:
                    log_engine(
                        f"OrderReconcilerWorker released gate reservation "
                        f"epic={epic} risk_gbp={released:.2f}"
                    )
            except Exception as e:
                log_engine(
                    f"OrderReconcilerWorker stale release failed epic={epic}: "
                    f"{type(e).__name__}: {e}"
                )
            resolve_pending(
                epic,
                reason=(
                    f"no broker position after {age:.0f}s — "
                    f"{PENDING_RECONCILE if pending.pending_reconcile else 'pending'} cleared"
                ),
            )
            return True
        return False

    # Exit pending: clear when broker shows flat.
    if not _broker_position_present(rest_client, epic, pending.side):
        resolve_pending(epic, reason="exit confirmed by OrderReconcilerWorker")
        return True
    return False


def reconcile_all_pending_orders(
    rest_client: Any,
    *,
    config: Any | None = None,
    stale_entry_grace_sec: float = DEFAULT_PENDING_TIMEOUT_SEC,
) -> int:
    """Run one reconcile pass over all pending orders. Returns clears."""
    cleared = 0
    for pending in list_pending_orders():
        if get_pending(pending.epic) is None:
            continue
        if reconcile_pending_order(
            rest_client,
            pending,
            config=config,
            stale_entry_grace_sec=stale_entry_grace_sec,
        ):
            cleared += 1
    return cleared


class OrderReconcilerWorker:
    """30-second scavenger loop for PENDING_RECONCILE broker ambiguity."""

    def __init__(
        self,
        rest_client: Any,
        *,
        config: Any | None = None,
        interval_seconds: float = RECONCILE_INTERVAL_SEC,
    ) -> None:
        self._rest = rest_client
        self._config = config
        self._interval = max(5.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="OrderReconcilerWorker",
        )
        self._thread.start()
        log_engine(
            f"OrderReconcilerWorker started (interval={self._interval:.0f}s)"
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 2.0)
        self._thread = None

    def tick_once(self) -> int:
        """Single reconcile pass (tests / manual invoke)."""
        with RECONCILE_TASK_GUARD.guarded_run() as active:
            if not active:
                return 0
            try:
                return reconcile_all_pending_orders(
                    self._rest,
                    config=self._config,
                )
            except Exception as e:
                log_engine(
                    f"OrderReconcilerWorker tick failed: {type(e).__name__}: {e}"
                )
                return 0

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick_once()


def start_order_reconciler_worker(
    rest_client: Any,
    *,
    config: Any | None = None,
    interval_seconds: float = RECONCILE_INTERVAL_SEC,
) -> OrderReconcilerWorker | None:
    global _worker_ref
    if rest_client is None:
        return None
    try:
        worker = OrderReconcilerWorker(
            rest_client,
            config=config,
            interval_seconds=interval_seconds,
        )
        worker.start()
        _worker_ref = worker
        return worker
    except Exception as e:
        log_engine(
            f"OrderReconcilerWorker start failed: {type(e).__name__}: {e}"
        )
        return None


def stop_order_reconciler_worker(worker: OrderReconcilerWorker | None = None) -> None:
    global _worker_ref
    target = worker if worker is not None else _worker_ref
    if target is None:
        return
    try:
        target.stop()
    except Exception as e:
        log_engine(f"OrderReconcilerWorker stop failed: {type(e).__name__}: {e}")
    if target is _worker_ref:
        _worker_ref = None


def reset_order_reconciler_worker_for_tests() -> None:
    stop_order_reconciler_worker()
