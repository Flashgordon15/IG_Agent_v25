"""
Active lifecycle trade registry — broker-authoritative open-position control plane.

Every open IG deal is adopted into local management, logged to ``active_lifecycle.log``,
and tracked until closed. Prevents drift between broker state, LearningStore, and triage.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analytics.triage_db import connect_triage_sqlite
from system.engine_log import log_engine
from system.paths import logs_dir
from system.trade_audit import log_trade_audit
from system.trade_lifecycle_bus import (
    STAGE_POSITION_OPENED,
    STAGE_POSITION_TRACKING,
    STATUS_OK,
    get_lifecycle_bus,
)

_LIFECYCLE_LOG = logs_dir() / "active_lifecycle.log"
_LOCK = threading.RLock()

STATE_DISCOVERED = "DISCOVERED"
STATE_ADOPTED = "ADOPTED"
STATE_AGENT_MANAGED = "AGENT_MANAGED"
STATE_CLOSED = "CLOSED"
STATE_BROKER_ANOMALY = "CLOSED_ON_BROKER_ANOMALY"

_TRIAGE_MANAGED = "AGENT_MANAGED"

_DDL = """
CREATE TABLE IF NOT EXISTS active_lifecycle_trades (
    deal_id TEXT PRIMARY KEY,
    trade_id INTEGER,
    epic TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL NOT NULL DEFAULT 0,
    lifecycle_state TEXT NOT NULL,
    broker_level REAL,
    broker_stop REAL,
    broker_limit REAL,
    broker_upl REAL,
    last_broker_sync_at TEXT NOT NULL,
    last_event TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_active_lifecycle_epic
    ON active_lifecycle_trades(epic);
CREATE INDEX IF NOT EXISTS idx_active_lifecycle_state
    ON active_lifecycle_trades(lifecycle_state);
"""


@dataclass(frozen=True)
class LifecycleAdoptionResult:
    deal_id: str
    trade_id: int | None
    state: str
    adopted: bool
    event: str


def _triage_db_path() -> Path:
    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_lifecycle_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def log_active_lifecycle(event: str, **fields: Any) -> None:
    """Append structured line to active_lifecycle.log (IG trading active log)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {k: v for k, v in fields.items() if v is not None}
    line = f"{ts} | {event} | {json.dumps(payload, default=str)}\n"
    try:
        from system.log_rotator import rotate_if_needed

        _LIFECYCLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_needed(_LIFECYCLE_LOG)
        with open(_LIFECYCLE_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        log_engine(f"active_lifecycle log write failed: {type(exc).__name__}: {exc}")
    log_trade_audit(f"lifecycle_{event}", **fields)


def _upsert_registry_row(
    conn: sqlite3.Connection,
    *,
    deal_id: str,
    trade_id: int | None,
    epic: str,
    direction: str,
    size: float,
    state: str,
    broker_level: float | None,
    broker_stop: float | None,
    broker_limit: float | None,
    broker_upl: float | None,
    event: str,
    notes: str = "",
) -> None:
    ensure_lifecycle_table(conn)
    conn.execute(
        """
        INSERT INTO active_lifecycle_trades (
            deal_id, trade_id, epic, direction, size, lifecycle_state,
            broker_level, broker_stop, broker_limit, broker_upl,
            last_broker_sync_at, last_event, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(deal_id) DO UPDATE SET
            trade_id = COALESCE(excluded.trade_id, active_lifecycle_trades.trade_id),
            epic = excluded.epic,
            direction = excluded.direction,
            size = excluded.size,
            lifecycle_state = excluded.lifecycle_state,
            broker_level = excluded.broker_level,
            broker_stop = excluded.broker_stop,
            broker_limit = excluded.broker_limit,
            broker_upl = excluded.broker_upl,
            last_broker_sync_at = excluded.last_broker_sync_at,
            last_event = excluded.last_event,
            notes = CASE
                WHEN excluded.notes != '' THEN excluded.notes
                ELSE active_lifecycle_trades.notes
            END
        """,
        (
            str(deal_id),
            int(trade_id) if trade_id is not None else None,
            str(epic),
            str(direction).upper(),
            float(size),
            str(state),
            broker_level,
            broker_stop,
            broker_limit,
            broker_upl,
            _now_iso(),
            str(event),
            str(notes or ""),
        ),
    )
    conn.commit()


def _mark_triage_agent_managed(deal_id: str) -> None:
    db = _triage_db_path()
    if not db.is_file():
        return
    try:
        conn = connect_triage_sqlite(db)
        conn.execute(
            """
            UPDATE production_orders
            SET status = ?
            WHERE deal_id = ? AND status NOT IN (?, 'FAILED', 'CLOSED_ON_BROKER_ANOMALY')
            """,
            (_TRIAGE_MANAGED, str(deal_id), _TRIAGE_MANAGED),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log_engine(
            f"active_lifecycle: triage link failed deal={deal_id}: "
            f"{type(exc).__name__}: {exc}"
        )


def _register_lifecycle_bus(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    market: str,
    trade_id: int | None,
) -> None:
    bus = get_lifecycle_bus()
    bus.begin_trade(epic=epic, direction=direction, market=market)
    bus.emit(
        STAGE_POSITION_OPENED,
        STATUS_OK,
        f"adopted broker position deal={deal_id}",
        deal_id=deal_id,
        trade_id=trade_id,
        epic=epic,
        direction=direction,
        source="active_lifecycle_adopt",
    )
    bus.emit(
        STAGE_POSITION_TRACKING,
        STATUS_OK,
        "agent-managed lifecycle active",
        deal_id=deal_id,
        trade_id=trade_id,
        epic=epic,
    )


def _broker_row_fields(pos: Any) -> dict[str, Any]:
    if hasattr(pos, "deal_id"):
        return {
            "deal_id": str(pos.deal_id),
            "epic": str(pos.epic),
            "direction": str(pos.direction),
            "size": float(pos.size),
            "level": float(pos.level),
            "stop": float(pos.stop_level or 0) or None,
            "limit": float(pos.limit_level or 0) or None,
            "upl": float(pos.upl) if pos.upl is not None else None,
            "market": str(pos.market_name or pos.epic),
            "deal_reference": str(pos.deal_reference or ""),
        }
    item = pos if isinstance(pos, dict) else {}
    p = item.get("position") or {}
    m = item.get("market") or {}
    upl = p.get("upl")
    return {
        "deal_id": str(p.get("dealId") or p.get("dealID") or ""),
        "epic": str(m.get("epic") or ""),
        "direction": str(p.get("direction") or "BUY"),
        "size": float(p.get("size") or 0),
        "level": float(p.get("level") or p.get("openLevel") or 0),
        "stop": float(p.get("stopLevel") or 0) or None,
        "limit": float(p.get("limitLevel") or 0) or None,
        "upl": float(upl) if upl is not None else None,
        "market": str(m.get("instrumentName") or m.get("epic") or ""),
        "deal_reference": str(p.get("dealReference") or ""),
    }


def adopt_broker_position(
    store: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    level: float,
    size: float,
    stop_level: float = 0.0,
    limit_level: float = 0.0,
    deal_reference: str = "",
    market: str = "",
    upl: float | None = None,
    source: str = "broker_sync",
) -> LifecycleAdoptionResult:
    """Ensure a broker open deal has a local trade row and lifecycle registry entry."""
    want = str(deal_id or "").strip()
    if not want or not epic:
        return LifecycleAdoptionResult(
            deal_id=want, trade_id=None, state="", adopted=False, event="skip_invalid"
        )

    with _LOCK:
        trade_id: int | None = None
        if hasattr(store, "find_open_by_deal_id"):
            row = store.find_open_by_deal_id(want)
            if row is not None:
                trade_id = int(row["id"])
        if trade_id is None and deal_reference and hasattr(
            store, "find_open_by_deal_reference"
        ):
            row = store.find_open_by_deal_reference(deal_reference)
            if row is not None:
                trade_id = int(row["id"])
                if hasattr(store, "set_ig_deal_id"):
                    store.set_ig_deal_id(trade_id, want)

        state = STATE_AGENT_MANAGED if trade_id else STATE_DISCOVERED
        adopted = False

        if trade_id is None and hasattr(store, "import_ig_position"):
            trade_id = int(
                store.import_ig_position(
                    epic=str(epic),
                    market=str(market or epic),
                    side=str(direction).upper(),
                    entry=float(level),
                    size=float(size),
                    deal_id=want,
                    deal_reference=str(deal_reference or ""),
                    notes=f"active_lifecycle: adopted from broker ({source})",
                    stop_level=float(stop_level or 0),
                    limit_level=float(limit_level or 0),
                )
            )
            state = STATE_ADOPTED
            adopted = True
            _register_lifecycle_bus(
                deal_id=want,
                epic=str(epic),
                direction=str(direction).upper(),
                market=str(market or epic),
                trade_id=trade_id,
            )
        elif trade_id is not None:
            state = STATE_AGENT_MANAGED
            if hasattr(store, "update_trade_upl") and upl is not None:
                store.update_trade_upl(trade_id, float(upl), float(level))

        conn = getattr(store, "conn", None)
        if conn is not None:
            _upsert_registry_row(
                conn,
                deal_id=want,
                trade_id=trade_id,
                epic=str(epic),
                direction=str(direction).upper(),
                size=float(size),
                state=state,
                broker_level=float(level),
                broker_stop=float(stop_level) if stop_level else None,
                broker_limit=float(limit_level) if limit_level else None,
                broker_upl=upl,
                event="adopted" if adopted else "synced",
                notes=f"source={source}",
            )

        _mark_triage_agent_managed(want)

        log_active_lifecycle(
            "adopted" if adopted else "synced",
            deal_id=want,
            trade_id=trade_id,
            epic=epic,
            direction=direction,
            size=size,
            lifecycle_state=state,
            broker_level=level,
            broker_upl=upl,
            source=source,
        )

        return LifecycleAdoptionResult(
            deal_id=want,
            trade_id=trade_id,
            state=state,
            adopted=adopted,
            event="adopted" if adopted else "synced",
        )


def close_lifecycle_deal(
    store: Any,
    *,
    deal_id: str,
    reason: str = "broker_closed",
) -> None:
    want = str(deal_id or "").strip()
    if not want:
        return
    conn = getattr(store, "conn", None)
    if conn is not None:
        ensure_lifecycle_table(conn)
        conn.execute(
            """
            UPDATE active_lifecycle_trades
            SET lifecycle_state = ?, last_broker_sync_at = ?, last_event = ?, notes = ?
            WHERE deal_id = ?
            """,
            (STATE_CLOSED, _now_iso(), "closed", str(reason), want),
        )
        conn.commit()
    log_active_lifecycle("closed", deal_id=want, reason=reason)


def reconcile_active_lifecycle_trades(
    store: Any,
    broker_positions: list[Any],
    *,
    source: str = "ig_position_sync",
) -> dict[str, int]:
    """
    Broker-authoritative adoption pass — call after every GET /positions reconcile.

    Returns counts: adopted, synced, closed_registry.
    """
    counts = {"adopted": 0, "synced": 0, "closed_registry": 0}
    if store is None:
        return counts

    broker_deals: set[str] = set()
    for pos in broker_positions or []:
        fields = _broker_row_fields(pos)
        did = str(fields.get("deal_id") or "")
        if not did or float(fields.get("size") or 0) <= 0:
            continue
        broker_deals.add(did)
        result = adopt_broker_position(
            store,
            deal_id=did,
            epic=str(fields["epic"]),
            direction=str(fields["direction"]),
            level=float(fields["level"]),
            size=float(fields["size"]),
            stop_level=float(fields["stop"] or 0),
            limit_level=float(fields["limit"] or 0),
            deal_reference=str(fields.get("deal_reference") or ""),
            market=str(fields.get("market") or fields["epic"]),
            upl=fields.get("upl"),
            source=source,
        )
        if result.adopted:
            counts["adopted"] += 1
        else:
            counts["synced"] += 1

    conn = getattr(store, "conn", None)
    if conn is not None:
        ensure_lifecycle_table(conn)
        rows = conn.execute(
            """
            SELECT deal_id FROM active_lifecycle_trades
            WHERE lifecycle_state NOT IN (?, ?)
            """,
            (STATE_CLOSED, STATE_BROKER_ANOMALY),
        ).fetchall()
        for (did,) in rows:
            if str(did) not in broker_deals:
                close_lifecycle_deal(store, deal_id=str(did), reason="absent_on_broker")
                counts["closed_registry"] += 1

    if counts["adopted"] or counts["closed_registry"]:
        log_engine(
            f"active_lifecycle reconcile: adopted={counts['adopted']} "
            f"synced={counts['synced']} closed_registry={counts['closed_registry']} "
            f"broker_open={len(broker_deals)}"
        )
    return counts


def boot_reconcile_active_trades(rest_client: Any, store: Any) -> dict[str, int]:
    """One-shot boot adoption — full broker ledger → lifecycle registry."""
    if rest_client is None or store is None:
        return {"adopted": 0, "synced": 0, "closed_registry": 0}
    if not hasattr(rest_client, "open_positions"):
        return {"adopted": 0, "synced": 0, "closed_registry": 0}
    try:
        raw = rest_client.open_positions() or []
        return reconcile_active_lifecycle_trades(
            store, raw, source="boot_reconcile"
        )
    except Exception as exc:
        log_engine(
            f"active_lifecycle boot reconcile failed: {type(exc).__name__}: {exc}"
        )
        return {"adopted": 0, "synced": 0, "closed_registry": 0}


def list_active_lifecycle_trades(store: Any) -> list[dict[str, Any]]:
    conn = getattr(store, "conn", None)
    if conn is None:
        return []
    ensure_lifecycle_table(conn)
    rows = conn.execute(
        """
        SELECT * FROM active_lifecycle_trades
        WHERE lifecycle_state IN (?, ?, ?)
        ORDER BY last_broker_sync_at DESC
        """,
        (STATE_DISCOVERED, STATE_ADOPTED, STATE_AGENT_MANAGED),
    ).fetchall()
    return [dict(r) for r in rows]
