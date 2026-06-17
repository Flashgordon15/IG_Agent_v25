"""
Live autonomous shadow trading engine — simulated fills when IG_AGENT_MODE=SHADOW.

Intercepts finalized 2-decimal truncated market signals and logs simulated fills
to ``shadow_ledger.jsonl``. Mark-to-market P&L refreshed from hub quotes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from execution.types import ExecutionResult, TradeSignal
from system.engine_log import log_engine
from system.paths import data_dir
from trading.position_ladder import finalize_dispatch_lot_size, truncate_to_broker_lot

_LEDGER_LOCK = threading.Lock()
_OPEN_LOCK = threading.Lock()
_OPEN_POSITIONS: dict[str, dict[str, Any]] = {}


def shadow_mode_active() -> bool:
    return os.environ.get("IG_AGENT_MODE", "").strip().upper() == "SHADOW"


def shadow_ledger_path() -> Path:
    path = data_dir() / "shadow_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_ledger_row(row: dict[str, Any]) -> None:
    path = shadow_ledger_path()
    with _LEDGER_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")


@dataclass
class ShadowFillResult:
    deal_id: str
    epic: str
    side: str
    size: float
    entry: float
    ts: float
    execution_params: dict[str, Any] = field(default_factory=dict)


class ShadowExecutor:
    """Simulated execution router — no broker REST calls."""

    def execute(
        self,
        signal: TradeSignal,
        execution_params: dict[str, Any],
    ) -> ExecutionResult:
        epic = str(signal.epic or "").strip()
        side = str(signal.direction or "").upper()
        entry = float(execution_params.get("entry") or execution_params.get("level") or 0)
        if entry <= 0:
            entry = float(getattr(signal, "entry_price", 0) or 0)

        raw_size = float(execution_params.get("size") or execution_params.get("dealSize") or 0)
        size, lot_note = finalize_dispatch_lot_size(
            raw_size,
            epic=epic,
            gate_approved_size=execution_params.get("gate_approved_size"),
        )
        size = truncate_to_broker_lot(size)
        if size <= 0:
            return ExecutionResult(
                success=False,
                action="REJECTED",
                rejection_reason="shadow: invalid lot size",
                execution_params=execution_params,
            )

        deal_id = f"SHADOW-{uuid.uuid4().hex[:12].upper()}"
        ts = time.time()
        fill = ShadowFillResult(
            deal_id=deal_id,
            epic=epic,
            side=side,
            size=size,
            entry=entry,
            ts=ts,
            execution_params=dict(execution_params),
        )
        row = {
            "event": "shadow_fill",
            "ts": ts,
            "deal_id": deal_id,
            "epic": epic,
            "side": side,
            "size": size,
            "entry": entry,
            "lot_note": lot_note,
            "mode": "SHADOW",
        }
        _append_ledger_row(row)
        with _OPEN_LOCK:
            _OPEN_POSITIONS[deal_id] = {
                "deal_id": deal_id,
                "epic": epic,
                "side": side,
                "size": size,
                "entry": entry,
                "open_ts": ts,
                "unrealized_gbp": 0.0,
            }
        log_engine(
            f"shadow_executor: simulated {side} {size} {epic} @ {entry} deal={deal_id}"
        )
        return ExecutionResult(
            success=True,
            action="SHADOW_FILL",
            deal_id=deal_id,
            execution_params={**execution_params, "shadow": True, "deal_id": deal_id},
            messages=[f"Shadow fill logged ({lot_note})"],
        )


def _quote_mid(epic: str) -> float:
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        q = hub.get_snapshot(epic) if hub else None
        if q is not None and q.bid > 0 and q.offer > q.bid:
            return (q.bid + q.offer) / 2.0
    except Exception:
        pass
    return 0.0


def _estimate_pnl_gbp(side: str, entry: float, mark: float, size: float, epic: str) -> float:
    if entry <= 0 or mark <= 0 or size <= 0:
        return 0.0
    diff = mark - entry if side == "BUY" else entry - mark
    epic_u = str(epic).upper()
    if "EUR" in epic_u:
        return diff * size * 10000.0 * 0.01
    if "GOLD" in epic_u or "CFP" in epic_u:
        return diff * size
    return diff * size * 0.1


def refresh_shadow_mtm() -> dict[str, Any]:
    """Mark open shadow positions to market from hub quotes (2.5 Hz telemetry path)."""
    if not shadow_mode_active() and not _OPEN_POSITIONS:
        return _read_ledger_summary()

    realized = _read_realized_pnl()
    open_rows: list[dict[str, Any]] = []
    unrealized_total = 0.0
    with _OPEN_LOCK:
        items = list(_OPEN_POSITIONS.items())
    for deal_id, pos in items:
        epic = str(pos.get("epic") or "")
        mark = _quote_mid(epic)
        upl = _estimate_pnl_gbp(
            str(pos.get("side") or ""),
            float(pos.get("entry") or 0),
            mark,
            float(pos.get("size") or 0),
            epic,
        )
        unrealized_total += upl
        open_rows.append(
            {
                "deal_id": deal_id,
                "epic": epic,
                "side": pos.get("side"),
                "size": pos.get("size"),
                "entry": pos.get("entry"),
                "mark": mark,
                "unrealized_gbp": round(upl, 2),
            }
        )
    return {
        "mode": "SHADOW" if shadow_mode_active() else "OFF",
        "open_count": len(open_rows),
        "open_positions": open_rows,
        "unrealized_gbp": round(unrealized_total, 2),
        "realized_gbp": round(realized, 2),
        "total_gbp": round(realized + unrealized_total, 2),
        "ledger_path": str(shadow_ledger_path()),
        "ts": time.time(),
    }


def _read_realized_pnl() -> float:
    path = shadow_ledger_path()
    if not path.is_file():
        return 0.0
    total = 0.0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("event") == "shadow_close":
                    total += float(row.get("realized_gbp") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return total


def _read_ledger_summary() -> dict[str, Any]:
    realized = _read_realized_pnl()
    return {
        "mode": "SHADOW" if shadow_mode_active() else "OFF",
        "open_count": 0,
        "open_positions": [],
        "unrealized_gbp": 0.0,
        "realized_gbp": round(realized, 2),
        "total_gbp": round(realized, 2),
        "ledger_path": str(shadow_ledger_path()),
        "ts": time.time(),
    }


def reset_shadow_executor_for_tests() -> None:
    with _OPEN_LOCK:
        _OPEN_POSITIONS.clear()
    path = shadow_ledger_path()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
