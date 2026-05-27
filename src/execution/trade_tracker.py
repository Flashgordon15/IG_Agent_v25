"""
Trade tracker — open-position counts and P&L from IG sync (preferred) or local store.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runtime.ig_position_sync import IgPositionSync


class TradeTracker:
    """Unified view of open positions for validation and UI — IG state when sync attached."""

    def __init__(
        self,
        store: Any,
        *,
        position_sync: IgPositionSync | None = None,
        prefer_ig: bool = True,
    ) -> None:
        self._store = store
        self._position_sync = position_sync
        self._prefer_ig = prefer_ig
        self._lock = threading.Lock()

    def attach_sync(self, position_sync: IgPositionSync | None) -> None:
        with self._lock:
            self._position_sync = position_sync

    def uses_ig_state(self) -> bool:
        return bool(self._prefer_ig and self._position_sync)

    def count_open_for_epic(self, epic: str) -> int:
        local = self._store.count_open_trades(epic) if self._store else 0
        if self.uses_ig_state():
            ig = self._position_sync.count_for_epic(epic)  # type: ignore[union-attr]
            if self._position_sync.is_fresh():  # type: ignore[union-attr]
                return ig
            return max(local, ig)
        return local

    def count_open_total(self) -> int:
        local = self._store.count_open_trades() if self._store else 0
        if self.uses_ig_state():
            ig = self._position_sync.total_open()  # type: ignore[union-attr]
            if self._position_sync.is_fresh():  # type: ignore[union-attr]
                return ig
            return max(local, ig)
        return local

    def snapshot(self) -> dict[str, Any]:
        sync_snap = self._position_sync.snapshot_dict() if self._position_sync else {}
        last_closed = self._store.get_last_closed_trade()
        closed_summary = sync_snap.get("last_closed_summary", "")
        if last_closed and not closed_summary:
            closed_summary = (
                f"{last_closed.get('epic')} {last_closed.get('side')} "
                f"{last_closed.get('result')} {float(last_closed.get('pnl_points') or 0):+.1f}pts"
            )
        return {
            "source": sync_snap.get("source", "store") if self.uses_ig_state() else "store",
            "total_open": self.count_open_total(),
            "by_epic": sync_snap.get("by_epic", {}),
            "account_upl": sync_snap.get("account_upl"),
            "last_sync_at": sync_snap.get("last_sync_at", ""),
            "sync_status": sync_snap.get("sync_status", ""),
            "rate_limit_paused": sync_snap.get("rate_limit_paused", False),
            "last_ig_event": sync_snap.get("last_ig_event", ""),
            "last_closed_summary": closed_summary,
            "positions": sync_snap.get("positions", []),
            "ig_fresh": self._position_sync.is_fresh() if self._position_sync else False,
        }
