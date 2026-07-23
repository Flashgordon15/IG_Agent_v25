"""EDITS_ONLY / instrument-restriction suspension — non-blocking fail-closed lane.

When IG marks a market EDITS_ONLY (or equivalent restriction), entry + sync
flatten paths raise ``InstrumentSuspendedException``. Deal/epic state flips to
``SUSPENDED`` without freezing dual-core worker threads. A 10s async recovery
poll re-checks tradeability and re-arms 3.5× ATR take-profits when cleared.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ig_api.exceptions import InstrumentSuspendedException
from system.engine_log import log_engine

RECOVERY_POLL_SEC = 10.0
ATR_TP_MULTIPLE = 3.5
# Tests may shrink the wait via IG_SUSPENSION_RECOVERY_SEC without touching prod cadence.
def _recovery_poll_sec() -> float:
    try:
        import os

        raw = os.environ.get("IG_SUSPENSION_RECOVERY_SEC", "").strip()
        if raw:
            return max(0.05, float(raw))
    except Exception:
        pass
    return RECOVERY_POLL_SEC

_RESTRICTION_MARKERS = (
    "EDITS_ONLY",
    "MARKET_CLOSED_WITH_EDITS",
    "NOT TRADEABLE",
    "MARKET NOT TRADEABLE",
    "MARKET_CLOSED",
    "MARKET CLOSED",
    "INSTRUMENT RESTRICTED",
    "TRANSACTION BLOCKED",
    "TRANSACTIONS BLOCKED",
    "MARKET RESTRICTED",
    "STATUS=SUSPENDED",
    "STATUS=CLOSED",
    "STATUS=OFFLINE",
    "STATUS=EDITS_ONLY",
)

_lock = threading.RLock()
_epic_suspended: dict[str, dict[str, Any]] = {}
_deal_suspended: dict[str, dict[str, Any]] = {}
_recovery_thread: threading.Thread | None = None
_recovery_stop = threading.Event()
_rest_client: Any | None = None


def reset_instrument_suspension_for_tests() -> None:
    """Unit-test hook — clear registries and stop recovery thread."""
    global _recovery_thread, _rest_client
    _recovery_stop.set()
    t = _recovery_thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=1.0)
    with _lock:
        _epic_suspended.clear()
        _deal_suspended.clear()
        _recovery_thread = None
        _rest_client = None
    _recovery_stop.clear()


def is_instrument_restriction(exc_or_msg: Any, *, status: str | None = None) -> bool:
    """True when broker text/status indicates EDITS_ONLY or market restriction."""
    if status:
        st = str(status).strip().upper()
        if st in {"EDITS_ONLY", "CLOSED", "SUSPENDED", "OFFLINE", "MARKET_CLOSED_WITH_EDITS"}:
            return True
        if st and st not in {"TRADEABLE", "OPEN", ""}:
            # Non-tradeable statuses other than empty probe failures
            if st in {"AUCTION", "ONAUCTION", "UNAVAILABLE"}:
                return True
    text = str(exc_or_msg or "").upper()
    if not text:
        return False
    return any(m in text for m in _RESTRICTION_MARKERS)


def raise_instrument_suspended(
    epic: str,
    *,
    status: str = "EDITS_ONLY",
    detail: str = "",
    deal_id: str | None = None,
    status_code: int | None = 400,
    body: str | None = None,
) -> None:
    """Mark SUSPENDED and raise — never returns."""
    mark_epic_suspended(epic, status=status, detail=detail, deal_id=deal_id)
    if deal_id:
        mark_deal_suspended(
            deal_id,
            epic=epic,
            status=status,
            detail=detail,
        )
    msg = detail or f"Market {epic} not tradeable (status={status})"
    raise InstrumentSuspendedException(
        msg,
        epic=epic,
        status=status,
        deal_id=deal_id,
        status_code=status_code,
        body=body,
    )


def mark_epic_suspended(
    epic: str,
    *,
    status: str = "EDITS_ONLY",
    detail: str = "",
    deal_id: str | None = None,
) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        row = _epic_suspended.get(key) or {}
        row.update(
            {
                "epic": key,
                "status": str(status or "EDITS_ONLY").upper(),
                "detail": str(detail or "")[:240],
                "mode": "SUSPENDED",
                "since": float(row.get("since") or time.time()),
                "updated_at": time.time(),
            }
        )
        if deal_id:
            deals = set(row.get("deal_ids") or [])
            deals.add(str(deal_id))
            row["deal_ids"] = sorted(deals)
        _epic_suspended[key] = row
    _ensure_recovery_thread()
    log_engine(
        f"InstrumentSuspension: epic={key} → SUSPENDED status={status} "
        f"deal={str(deal_id or '')[:12]}"
    )


def mark_deal_suspended(
    deal_id: str,
    *,
    epic: str,
    status: str = "EDITS_ONLY",
    detail: str = "",
    entry_level: float = 0.0,
    direction: str = "BUY",
    size: float = 0.0,
) -> None:
    did = str(deal_id or "").strip()
    if not did:
        return
    with _lock:
        prev = _deal_suspended.get(did) or {}
        _deal_suspended[did] = {
            "deal_id": did,
            "epic": str(epic or prev.get("epic") or "").strip(),
            "status": str(status or "EDITS_ONLY").upper(),
            "detail": str(detail or prev.get("detail") or "")[:240],
            "mode": "SUSPENDED",
            "since": float(prev.get("since") or time.time()),
            "updated_at": time.time(),
            "entry_level": float(entry_level or prev.get("entry_level") or 0.0),
            "direction": str(direction or prev.get("direction") or "BUY").upper(),
            "size": float(size or prev.get("size") or 0.0),
        }
    mark_epic_suspended(epic, status=status, detail=detail, deal_id=did)


def is_epic_suspended(epic: str) -> bool:
    key = str(epic or "").strip()
    with _lock:
        return key in _epic_suspended


def is_deal_suspended(deal_id: str) -> bool:
    did = str(deal_id or "").strip()
    with _lock:
        return did in _deal_suspended


def suspended_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "epics": {k: dict(v) for k, v in _epic_suspended.items()},
            "deals": {k: dict(v) for k, v in _deal_suspended.items()},
            "recovery_alive": bool(
                _recovery_thread is not None and _recovery_thread.is_alive()
            ),
        }


def clear_epic_suspension(epic: str) -> None:
    key = str(epic or "").strip()
    with _lock:
        _epic_suspended.pop(key, None)


def clear_deal_suspension(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _deal_suspended.pop(did, None)


def bind_rest_client(rest: Any | None) -> None:
    global _rest_client
    if rest is not None:
        _rest_client = rest
        _ensure_recovery_thread()


def handle_dispatch_suspension(exc: BaseException, *, epic: str) -> str:
    """
    Dual-core catch helper — mark SUSPENDED and return block reason.
    Never blocks; safe on worker threads.
    """
    status = getattr(exc, "status", None) or "EDITS_ONLY"
    detail = str(exc)
    mark_epic_suspended(epic, status=str(status), detail=detail)
    return f"instrument_suspended:{status}"


def _ensure_recovery_thread() -> None:
    global _recovery_thread
    with _lock:
        if _recovery_thread is not None and _recovery_thread.is_alive():
            return
        _recovery_stop.clear()
        _recovery_thread = threading.Thread(
            target=_recovery_loop,
            name="instrument-suspension-recovery",
            daemon=True,
        )
        _recovery_thread.start()


def _recovery_loop() -> None:
    while not _recovery_stop.wait(_recovery_poll_sec()):
        try:
            _recovery_tick()
        except Exception as exc:
            log_engine(
                f"InstrumentSuspension: recovery tick failed "
                f"{type(exc).__name__}: {exc}"
            )
        with _lock:
            if not _epic_suspended and not _deal_suspended:
                # Idle exit — thread will be recreated on next suspend
                break


def _recovery_tick() -> None:
    rest = _rest_client
    if rest is None:
        try:
            from runtime.trade_manager import get_dual_core_coordinator

            coord = get_dual_core_coordinator()
            rest = getattr(coord, "_rest", None) if coord else None
        except Exception:
            rest = None
    if rest is None:
        return

    with _lock:
        epics = list(_epic_suspended.keys())
        deals = [dict(v) for v in _deal_suspended.values()]

    if not epics and not deals:
        return

    try:
        from execution.broker_tradeability import (
            broker_market_status,
            clear_broker_status_cache,
        )
    except Exception:
        return

    # Force fresh probe for suspended epics
    try:
        clear_broker_status_cache()
    except Exception:
        pass

    cleared_epics: list[str] = []
    for epic in epics:
        try:
            status = str(broker_market_status(rest, epic) or "").upper()
        except Exception:
            status = ""
        if status in {"TRADEABLE", "OPEN"}:
            cleared_epics.append(epic)

    for epic in cleared_epics:
        log_engine(f"InstrumentSuspension: epic={epic} TRADEABLE — clearing SUSPENDED")
        clear_epic_suspension(epic)
        for row in deals:
            if str(row.get("epic") or "") != epic:
                continue
            _rearm_after_clear(rest, row)
            clear_deal_suspension(str(row.get("deal_id") or ""))

    # Drain queued EDITS_ONLY closes once any epic clears
    if cleared_epics:
        try:
            from execution.edits_only_close_queue import drain_when_tradeable

            out = drain_when_tradeable(rest, cfg=None)
            if out.get("attempted") or out.get("closed"):
                log_engine(
                    f"InstrumentSuspension: edits_only drain "
                    f"closed={out.get('closed')} pending={out.get('still_pending')}"
                )
        except Exception as exc:
            log_engine(
                f"InstrumentSuspension: drain failed {type(exc).__name__}: {exc}"
            )


def _rearm_after_clear(rest: Any, row: dict[str, Any]) -> None:
    """Re-arm GBP / virtual / dynamic (3.5× ATR TP path) for a cleared deal."""
    did = str(row.get("deal_id") or "").strip()
    epic = str(row.get("epic") or "").strip()
    if not did or not epic:
        return
    direction = str(row.get("direction") or "BUY").upper()
    size = float(row.get("size") or 0.0)
    entry = float(row.get("entry_level") or 0.0)

    # Refresh from live book when possible
    try:
        for item in rest.open_positions(budget_priority=True) or []:
            pos = (item or {}).get("position") or {}
            mid = str(pos.get("dealId") or pos.get("dealID") or "").strip()
            if mid != did:
                continue
            direction = str(pos.get("direction") or direction).upper()
            size = float(pos.get("size") or size or 0.0)
            entry = float(pos.get("level") or pos.get("openLevel") or entry or 0.0)
            mkt = (item or {}).get("market") or {}
            epic = str(mkt.get("epic") or epic).strip()
            break
        else:
            # Flat — nothing to re-arm
            return
    except Exception:
        pass

    if size <= 0 or entry <= 0:
        return

    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        cfg = None

    try:
        from execution.position_risk_arms import (
            arm_dynamic_limit_for_position,
            arm_gbp_exit_for_position,
            arm_virtual_stop_for_position,
            bind_risk_rest_clients,
        )

        bind_risk_rest_clients(rest)
        arm_gbp_exit_for_position(
            deal_id=did,
            epic=epic,
            direction=direction,
            size=size,
            entry_level=entry,
            cfg=cfg,
        )
        # Use configured virtual_stop_ceiling (12pt), not a hardcoded 4pt IG min —
        # short broker_stop_pts previously collapsed software ceiling to 3.4pt.
        broker_stop_pts = 12.0
        try:
            from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

            _, sl_pts, prof = resolve_micro_tp_sl_for_epic(epic, size, cfg)
            broker_stop_pts = max(
                float(prof.virtual_stop_ceiling_pts or 12.0),
                float(sl_pts or 0.0),
                12.0,
            )
        except Exception:
            broker_stop_pts = 12.0
        arm_virtual_stop_for_position(
            deal_id=did,
            epic=epic,
            direction=direction,
            size=size,
            entry_level=entry,
            broker_stop_pts=float(broker_stop_pts),
            cfg=cfg,
        )
        limit_pts = None
        try:
            from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

            tp_pts, _, _ = resolve_micro_tp_sl_for_epic(epic, size, cfg)
            # Prefer elevated 3.5× ATR-style target when profile TP is available
            limit_pts = max(float(tp_pts), float(tp_pts) * (ATR_TP_MULTIPLE / 2.0))
        except Exception:
            limit_pts = None
        arm_dynamic_limit_for_position(
            deal_id=did,
            epic=epic,
            direction=direction,
            size=size,
            entry_level=entry,
            broker_stop_pts=float(broker_stop_pts),
            limit_distance_pts=limit_pts,
            cfg=cfg,
            rest_client=rest,
        )
        log_engine(
            f"InstrumentSuspension: re-armed protection deal={did[:12]} epic={epic} "
            f"entry={entry:.1f} atr_tp_mult={ATR_TP_MULTIPLE}"
        )
    except Exception as exc:
        log_engine(
            f"InstrumentSuspension: re-arm failed deal={did[:12]} "
            f"{type(exc).__name__}: {exc}"
        )


def maybe_raise_from_error(
    exc_or_msg: Any,
    *,
    epic: str,
    deal_id: str | None = None,
    status: str | None = None,
) -> None:
    """If message matches restriction, convert to InstrumentSuspendedException."""
    if isinstance(exc_or_msg, InstrumentSuspendedException):
        raise exc_or_msg
    if not is_instrument_restriction(exc_or_msg, status=status):
        return
    st = status or "EDITS_ONLY"
    text = str(exc_or_msg)
    for marker in ("STATUS=", "status="):
        if marker in text:
            # parse status=FOO from existing messages
            try:
                part = text.split(marker, 1)[1]
                st = part.split(")", 1)[0].split(",", 1)[0].strip() or st
            except Exception:
                pass
            break
    raise_instrument_suspended(
        epic,
        status=st,
        detail=text,
        deal_id=deal_id,
        body=getattr(exc_or_msg, "body", None) if not isinstance(exc_or_msg, str) else None,
    )
