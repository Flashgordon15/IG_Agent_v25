"""
Trade lifecycle event bus — observability only; does not affect trading logic.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

STAGE_SIGNAL = "signal"
STAGE_VALIDATION = "validation"
STAGE_RISK = "risk"
STAGE_EXECUTION_REQUEST = "execution_request"
STAGE_IG_RESPONSE = "ig_response"
STAGE_POSITION_OPENED = "position_opened"
STAGE_POSITION_TRACKING = "position_tracking"
STAGE_POSITION_CLOSED = "position_closed"

LIFECYCLE_STAGES: tuple[str, ...] = (
    STAGE_SIGNAL,
    STAGE_VALIDATION,
    STAGE_RISK,
    STAGE_EXECUTION_REQUEST,
    STAGE_IG_RESPONSE,
    STAGE_POSITION_OPENED,
    STAGE_POSITION_TRACKING,
    STAGE_POSITION_CLOSED,
)

STAGE_LABELS: dict[str, str] = {
    STAGE_SIGNAL: "Signal Received",
    STAGE_VALIDATION: "Validation",
    STAGE_RISK: "Risk Engine",
    STAGE_EXECUTION_REQUEST: "Execution Request",
    STAGE_IG_RESPONSE: "IG REST Response",
    STAGE_POSITION_OPENED: "Position Opened (IG)",
    STAGE_POSITION_TRACKING: "Position Tracking",
    STAGE_POSITION_CLOSED: "Position Closed",
}

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_PENDING = "pending"

FINAL_IN_PROGRESS = "IN_PROGRESS"
FINAL_SUCCESS = "SUCCESS"
FINAL_FAILED = "FAILED"
FINAL_REJECTED = "REJECTED"


@dataclass
class StageUpdate:
    stage: str
    status: str
    message: str = ""
    timestamp: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LifecycleRecord:
    lifecycle_id: str
    epic: str
    direction: str
    market: str = ""
    deal_id: str = ""
    started_at: str = ""
    stages: dict[str, StageUpdate] = field(default_factory=dict)
    final_state: str = FINAL_IN_PROGRESS
    failed_stage: str = ""
    failure_reason: str = ""
    result: str = ""
    pnl: float = 0.0
    pnl_is_currency: bool = False
    duration_sec: float = 0.0
    closed_at: str = ""
    close_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "epic": self.epic,
            "direction": self.direction,
            "market": self.market,
            "deal_id": self.deal_id,
            "started_at": self.started_at,
            "stages": {k: _stage_dict(v) for k, v in self.stages.items()},
            "final_state": self.final_state,
            "failed_stage": self.failed_stage,
            "failure_reason": self.failure_reason,
            "result": self.result,
            "pnl": self.pnl,
            "pnl_is_currency": self.pnl_is_currency,
            "duration_sec": self.duration_sec,
            "closed_at": self.closed_at,
            "close_source": self.close_source,
        }


def _stage_dict(s: StageUpdate) -> dict[str, Any]:
    return {
        "stage": s.stage,
        "status": s.status,
        "message": s.message,
        "timestamp": s.timestamp,
        "extra": dict(s.extra),
    }


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


class TradeLifecycleBus:
    """Thread-safe pub/sub for trade lifecycle stages."""

    def __init__(self, history_size: int = 10) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._current: LifecycleRecord | None = None
        self._history: deque[LifecycleRecord] = deque(maxlen=history_size)
        self._started_mono: float | None = None

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def begin_trade(self, *, epic: str, direction: str, market: str = "") -> str:
        import time

        direction = direction.upper()
        with self._lock:
            if self._current and self._current.final_state == FINAL_IN_PROGRESS:
                if (
                    self._current.epic == epic
                    and self._current.direction == direction
                ):
                    return self._current.lifecycle_id
                self._drop_current_without_history()
            lid = uuid.uuid4().hex[:12]
            self._current = LifecycleRecord(
                lifecycle_id=lid,
                epic=epic,
                direction=direction,
                market=market,
                started_at=_now_str(),
            )
            self._started_mono = time.monotonic()
            for stage in LIFECYCLE_STAGES:
                self._current.stages[stage] = StageUpdate(
                    stage=stage,
                    status=STATUS_PENDING,
                    message="Waiting",
                    timestamp="",
                )
        self._notify()
        return lid

    def emit(
        self,
        stage_name: str,
        status: str,
        message: str = "",
        **extra: Any,
    ) -> None:
        """Emit lifecycle_update — stage_name, status (ok|fail|skip|pending), message."""
        if stage_name not in LIFECYCLE_STAGES:
            return
        with self._lock:
            if self._current is None and stage_name == STAGE_SIGNAL:
                epic = str(extra.get("epic", ""))
                direction = str(extra.get("direction", "WAIT"))
                market = str(extra.get("market", ""))
                if direction in ("BUY", "SELL"):
                    self.begin_trade(epic=epic, direction=direction, market=market)
            if self._current is None:
                return
            ts = _now_str()
            self._current.stages[stage_name] = StageUpdate(
                stage=stage_name,
                status=status,
                message=message,
                timestamp=ts,
                extra=dict(extra),
            )
            if stage_name == STAGE_POSITION_OPENED and status == STATUS_OK:
                did = str(extra.get("deal_id") or "")
                if did:
                    self._current.deal_id = did
            if status == STATUS_FAIL and self._current.final_state == FINAL_IN_PROGRESS:
                self._current.failed_stage = stage_name
                self._current.failure_reason = message
        self._notify()

    def record_validation_block(
        self,
        *,
        epic: str,
        direction: str,
        market: str,
        signal_message: str,
        reasons: list[str] | str,
    ) -> None:
        """
        Show live validation state in the UI without appending a failed trade to history.
        """
        reason = "; ".join(reasons) if isinstance(reasons, list) else str(reasons)
        with self._lock:
            if self._current and self._current.final_state == FINAL_IN_PROGRESS:
                self._drop_current_without_history()
            lid = uuid.uuid4().hex[:12]
            self._current = LifecycleRecord(
                lifecycle_id=lid,
                epic=epic,
                direction=direction.upper(),
                market=market,
                started_at=_now_str(),
                final_state=FINAL_REJECTED,
                failed_stage=STAGE_VALIDATION,
                failure_reason=reason,
            )
            for stage in LIFECYCLE_STAGES:
                st = STATUS_SKIP
                msg = "Not required"
                if stage == STAGE_SIGNAL:
                    st, msg = STATUS_OK, signal_message
                elif stage == STAGE_VALIDATION:
                    st, msg = STATUS_FAIL, reason
                self._current.stages[stage] = StageUpdate(
                    stage=stage,
                    status=st,
                    message=msg,
                    timestamp=_now_str() if st != STATUS_SKIP else "",
                )
        self._notify()

    def emit_lifecycle_update(
        self,
        stage_name: str,
        status: str,
        message: str = "",
        **extra: Any,
    ) -> None:
        """Alias matching spec: emit('lifecycle_update', stage_name, status, message)."""
        self.emit(stage_name, status, message, **extra)

    def mark_position_closed(
        self,
        *,
        message: str,
        result: str = "",
        pnl: float = 0.0,
        pnl_is_currency: bool = False,
        source: str = "ig",
        epic: str = "",
        direction: str = "",
        deal_id: str = "",
        trade_id: int | None = None,
    ) -> None:
        """
        Close the active lifecycle when it matches an opened position.

        IG sync / bot closes must not finalize a blocked signal lifecycle that
        never reached position_opened (common when sync closes stale DB rows).
        """
        with self._lock:
            cur = self._current
            if cur and self._can_close_current(
                cur, epic=epic, direction=direction, deal_id=deal_id
            ):
                self._apply_close_to_record(
                    cur,
                    message=message,
                    result=result,
                    pnl=pnl,
                    pnl_is_currency=pnl_is_currency,
                    source=source,
                )
                self._archive_current(final_state=FINAL_SUCCESS)
            elif epic or deal_id or (cur and self._position_was_opened(cur)):
                self._record_standalone_close(
                    epic=epic or (cur.epic if cur else ""),
                    direction=direction or (cur.direction if cur else ""),
                    message=message,
                    result=result,
                    pnl=pnl,
                    pnl_is_currency=pnl_is_currency,
                    source=source,
                    deal_id=deal_id,
                    trade_id=trade_id,
                )
        self._notify()

    def finalize_success(
        self,
        *,
        result: str = "",
        pnl: float = 0.0,
        pnl_is_currency: bool = False,
    ) -> None:
        with self._lock:
            if not self._current:
                return
            self._current.result = result or self._current.result
            self._current.pnl = pnl
            self._current.pnl_is_currency = pnl_is_currency
            self._archive_current(final_state=FINAL_SUCCESS)
        self._notify()

    def finalize_rejected(self, reason: str = "", *, stage: str = STAGE_VALIDATION) -> None:
        """Pre-trade block after lifecycle started — visible in UI, not added to history."""
        with self._lock:
            if not self._current:
                return
            if reason:
                self._current.failure_reason = reason
            self._current.failed_stage = stage
            self._current.final_state = FINAL_REJECTED
            ts = _now_str()
            self._current.stages[stage] = StageUpdate(
                stage=stage,
                status=STATUS_FAIL,
                message=reason,
                timestamp=ts,
            )
        self._notify()

    def finalize_failure(self, reason: str = "") -> None:
        """True execution / IG failure — recorded in history."""
        with self._lock:
            if not self._current:
                return
            if reason:
                self._current.failure_reason = reason
            if not self._current.failed_stage:
                self._current.failed_stage = STAGE_IG_RESPONSE
            self._archive_current(final_state=FINAL_FAILED)
        self._notify()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current": self._current.to_dict() if self._current else None,
                "history": [r.to_dict() for r in reversed(self._history)],
            }

    def get_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in reversed(self._history)]

    def _drop_current_without_history(self) -> None:
        """Clear in-progress lifecycle without polluting the history table."""
        self._current = None
        self._started_mono = None

    @staticmethod
    def _position_was_opened(rec: LifecycleRecord) -> bool:
        opened = rec.stages.get(STAGE_POSITION_OPENED)
        return bool(opened and opened.status == STATUS_OK)

    def _can_close_current(
        self,
        rec: LifecycleRecord,
        *,
        epic: str = "",
        direction: str = "",
        deal_id: str = "",
    ) -> bool:
        if rec.final_state in (FINAL_REJECTED, FINAL_FAILED):
            return False
        if not self._position_was_opened(rec):
            return False
        if epic and rec.epic and epic != rec.epic:
            return False
        if direction and rec.direction and direction.upper() != rec.direction.upper():
            return False
        rec_deal = rec.deal_id
        if not rec_deal:
            opened_stage = rec.stages.get(STAGE_POSITION_OPENED)
            if opened_stage:
                rec_deal = str(opened_stage.extra.get("deal_id") or "")
        if deal_id and rec_deal and deal_id != rec_deal:
            return False
        return True

    def _apply_close_to_record(
        self,
        rec: LifecycleRecord,
        *,
        message: str,
        result: str,
        pnl: float,
        pnl_is_currency: bool,
        source: str,
    ) -> None:
        ts = _now_str()
        rec.stages[STAGE_POSITION_CLOSED] = StageUpdate(
            stage=STAGE_POSITION_CLOSED,
            status=STATUS_OK,
            message=message,
            timestamp=ts,
            extra={"result": result, "pnl": pnl, "source": source},
        )
        rec.result = result or rec.result
        rec.pnl = pnl
        rec.pnl_is_currency = pnl_is_currency
        rec.close_source = source

    def _record_standalone_close(
        self,
        *,
        epic: str,
        direction: str,
        message: str,
        result: str,
        pnl: float,
        pnl_is_currency: bool,
        source: str,
        deal_id: str = "",
        trade_id: int | None = None,
    ) -> None:
        """Broker close with no matching in-flight lifecycle (e.g. stale DB row)."""
        import time

        ts = _now_str()
        rec = LifecycleRecord(
            lifecycle_id=uuid.uuid4().hex[:12],
            epic=epic,
            direction=(direction or "—").upper(),
            deal_id=deal_id,
            started_at=ts,
            final_state=FINAL_SUCCESS,
            result=result,
            pnl=pnl,
            pnl_is_currency=pnl_is_currency,
            closed_at=ts,
            close_source=source,
        )
        for stage in LIFECYCLE_STAGES:
            st = STATUS_SKIP
            msg = "Not required"
            if stage == STAGE_POSITION_OPENED:
                st, msg = STATUS_OK, f"Opened (sync) deal={deal_id or trade_id or '—'}"
            elif stage == STAGE_POSITION_CLOSED:
                st, msg = STATUS_OK, message
            rec.stages[stage] = StageUpdate(
                stage=stage,
                status=st,
                message=msg,
                timestamp=ts if st != STATUS_SKIP else "",
                extra={"source": source, "deal_id": deal_id, "trade_id": trade_id},
            )
        rec.duration_sec = 0.0
        self._history.append(rec)

    def _archive_current(self, *, final_state: str, failure_reason: str = "") -> None:
        import time

        if not self._current:
            return
        rec = self._current
        rec.final_state = final_state
        if failure_reason:
            rec.failure_reason = failure_reason
        rec.closed_at = _now_str()
        if self._started_mono is not None:
            rec.duration_sec = max(0.0, time.monotonic() - self._started_mono)
        self._history.append(rec)
        self._current = None
        self._started_mono = None

    def _notify(self) -> None:
        snap = self.snapshot()
        subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(snap)
            except Exception:
                pass


_bus: TradeLifecycleBus | None = None


def get_lifecycle_bus() -> TradeLifecycleBus:
    global _bus
    if _bus is None:
        _bus = TradeLifecycleBus()
    return _bus


def emit_lifecycle(stage_name: str, status: str, message: str = "", **extra: Any) -> None:
    get_lifecycle_bus().emit(stage_name, status, message, **extra)
