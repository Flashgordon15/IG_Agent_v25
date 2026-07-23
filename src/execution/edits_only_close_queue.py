"""Persist closes that failed because the market is EDITS_ONLY / not TRADEABLE.

TradeSupport and OPM drain this queue when the epic becomes TRADEABLE/OPEN again.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from system.paths import data_dir

_LOCK = threading.RLock()
_QUEUE_NAME = "edits_only_close_queue.json"
_MAX_ENTRIES = 64
_MAX_AGE_SEC = 72 * 3600


def _path() -> Any:
    return data_dir() / _QUEUE_NAME


def _is_edits_only_error(exc_or_msg: Any) -> bool:
    try:
        from execution.instrument_suspension import is_instrument_restriction

        return bool(is_instrument_restriction(exc_or_msg))
    except Exception:
        text = str(exc_or_msg or "")
        upper = text.upper()
        return (
            "EDITS_ONLY" in upper
            or "NOT TRADEABLE" in upper
            or "MARKET_CLOSED" in upper
        )


def load_queue() -> list[dict[str, Any]]:
    path = _path()
    try:
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = list(raw.get("pending") or []) if isinstance(raw, dict) else []
    except Exception:
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = float(row.get("ts") or 0)
        if ts > 0 and now - ts > _MAX_AGE_SEC:
            continue
        deal_id = str(row.get("deal_id") or "").strip()
        if not deal_id:
            continue
        out.append(row)
    return out[:_MAX_ENTRIES]


def _save(pending: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "pending": pending[:_MAX_ENTRIES]}
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def enqueue_close(
    *,
    deal_id: str,
    epic: str,
    direction: str = "BUY",
    size: float = 0.0,
    reason: str = "",
    error: str = "",
    pnl_gbp: float | None = None,
) -> bool:
    """Record a failed flatten for later retry. Returns True if newly queued."""
    did = str(deal_id or "").strip()
    if not did or not _is_edits_only_error(error):
        return False
    with _LOCK:
        pending = load_queue()
        for row in pending:
            if str(row.get("deal_id") or "") == did:
                row["ts"] = time.time()
                row["error"] = str(error)[:240]
                row["reason"] = str(reason or row.get("reason") or "")[:160]
                row["attempts"] = int(row.get("attempts") or 1)
                _save(pending)
                return False
        pending.append(
            {
                "ts": time.time(),
                "deal_id": did,
                "epic": str(epic or "").strip(),
                "direction": str(direction or "BUY").upper(),
                "size": float(size or 0),
                "reason": str(reason or "")[:160],
                "error": str(error)[:240],
                "pnl_gbp": pnl_gbp,
                "attempts": 1,
            }
        )
        _save(pending)
        return True


def remove_deal(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    if not did:
        return
    with _LOCK:
        pending = [r for r in load_queue() if str(r.get("deal_id") or "") != did]
        _save(pending)


def pending_count() -> int:
    return len(load_queue())


def drain_when_tradeable(rest: Any, cfg: Any | None = None) -> dict[str, Any]:
    """
    Retry queued closes for epics that are TRADEABLE/OPEN.
    Safe to call every trade_support / OPM cycle.
    """
    pending = load_queue()
    if not pending:
        return {"attempted": 0, "closed": 0, "still_pending": 0}

    try:
        from execution.broker_tradeability import broker_market_status
    except Exception:
        broker_market_status = None  # type: ignore

    closed = 0
    attempted = 0
    remaining: list[dict[str, Any]] = []

    for row in pending:
        deal_id = str(row.get("deal_id") or "").strip()
        epic = str(row.get("epic") or "").strip()
        if not deal_id:
            continue
        status = ""
        if broker_market_status and epic:
            try:
                status = str(broker_market_status(rest, epic, cfg=cfg) or "").upper()
            except Exception:
                status = ""
        if status and status not in ("TRADEABLE", "OPEN"):
            remaining.append(row)
            continue

        attempted += 1
        try:
            direction = str(row.get("direction") or "BUY").upper()
            size = float(row.get("size") or 0)
            # Refresh size/direction from book when possible.
            for item in rest.open_positions(budget_priority=True) or []:
                pos = item.get("position") or {}
                did = str(pos.get("dealId") or pos.get("dealID") or "").strip()
                if did != deal_id:
                    continue
                direction = str(pos.get("direction") or direction).upper()
                size = float(pos.get("size") or size or 0)
                mkt = item.get("market") or {}
                epic = str(mkt.get("epic") or epic).strip()
                break
            else:
                # Already gone — drop from queue.
                closed += 1
                continue

            # close_position(skip_lookup=True) inverts OPEN once — pass OPEN side.
            rest.close_position(
                deal_id,
                direction=direction,
                size=size,
                epic=epic,
                verify=False,
                budget_priority=True,
                skip_lookup=True,
                skip_confirm=True,
            )
            closed += 1
            try:
                from runtime.strategy_improvement_tracker import record_managed_close

                record_managed_close(
                    epic=epic,
                    pnl_gbp=float(row.get("pnl_gbp") or 0),
                    exit_reason=f"edits_only_queue:{row.get('reason') or 'retry'}",
                )
            except Exception:
                pass
        except Exception as exc:
            row = dict(row)
            row["ts"] = time.time()
            row["error"] = f"{type(exc).__name__}: {exc}"[:240]
            row["attempts"] = int(row.get("attempts") or 1) + 1
            if _is_edits_only_error(exc) or row["attempts"] < 40:
                remaining.append(row)

    with _LOCK:
        _save(remaining)

    return {
        "attempted": attempted,
        "closed": closed,
        "still_pending": len(remaining),
    }
