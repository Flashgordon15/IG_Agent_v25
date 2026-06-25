"""Torture / saturation test ledger — triage_v31.db extension tables."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from analytics.triage_db import connect_triage_sqlite

_TORTURE_DDL = """
CREATE TABLE IF NOT EXISTS torture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    epic TEXT,
    deal_id TEXT,
    direction TEXT,
    detail TEXT,
    rtt_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_torture_session ON torture_events(session_id);
CREATE INDEX IF NOT EXISTS idx_torture_type ON torture_events(event_type);

CREATE TABLE IF NOT EXISTS torture_sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    orders_ok INTEGER NOT NULL DEFAULT 0,
    orders_fail INTEGER NOT NULL DEFAULT 0,
    connection_drops INTEGER NOT NULL DEFAULT 0,
    trailing_mods INTEGER NOT NULL DEFAULT 0,
    scalp_exits INTEGER NOT NULL DEFAULT 0,
    share_min_pacing_ms REAL,
    share_max_pacing_ms REAL,
    report_json TEXT
);
"""


def torture_db_path() -> Path:
    import os

    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db"


def ensure_torture_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_TORTURE_DDL)


def record_event(
    *,
    session_id: str,
    event_type: str,
    epic: str | None = None,
    deal_id: str | None = None,
    direction: str | None = None,
    detail: Any = None,
    rtt_ms: float | None = None,
) -> None:
    db = torture_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_triage_sqlite(db)
    try:
        ensure_torture_schema(conn)
        conn.execute(
            """
            INSERT INTO torture_events
                (session_id, event_type, epic, deal_id, direction, detail, rtt_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event_type,
                epic,
                deal_id,
                direction,
                json.dumps(detail, default=str) if detail is not None else None,
                rtt_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_session(session_id: str, **fields: Any) -> None:
    db = torture_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_triage_sqlite(db)
    try:
        ensure_torture_schema(conn)
        row = conn.execute(
            "SELECT session_id FROM torture_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO torture_sessions (session_id, started_at)
                VALUES (?, ?)
                """,
                (session_id, time.time()),
            )
        sets = []
        vals: list[Any] = []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        if sets:
            vals.append(session_id)
            conn.execute(
                f"UPDATE torture_sessions SET {', '.join(sets)} WHERE session_id = ?",
                vals,
            )
        conn.commit()
    finally:
        conn.close()


def build_certification_report(session_id: str) -> dict[str, Any]:
    db = torture_db_path()
    if not db.is_file():
        return {"ok": False, "error": "triage db missing"}
    conn = connect_triage_sqlite(db)
    try:
        ensure_torture_schema(conn)
        sess = conn.execute(
            "SELECT * FROM torture_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        col_info = conn.execute("PRAGMA table_info(torture_sessions)").fetchall()
        cols = [c[1] for c in col_info]
        session_row = dict(zip(cols, sess, strict=False)) if sess else {}
        trailing = conn.execute(
            "SELECT COUNT(*) FROM torture_events WHERE session_id = ? AND event_type = 'trailing_mod'",
            (session_id,),
        ).fetchone()[0]
        scalp = conn.execute(
            "SELECT COUNT(*) FROM torture_events WHERE session_id = ? AND event_type = 'scalp_exit'",
            (session_id,),
        ).fetchone()[0]
        drops = conn.execute(
            "SELECT COUNT(*) FROM torture_events WHERE session_id = ? AND event_type = 'connection_drop'",
            (session_id,),
        ).fetchone()[0]
        ok_orders = conn.execute(
            "SELECT COUNT(*) FROM torture_events WHERE session_id = ? AND event_type = 'order_ok'",
            (session_id,),
        ).fetchone()[0]
        fail_orders = conn.execute(
            "SELECT COUNT(*) FROM torture_events WHERE session_id = ? AND event_type = 'order_fail'",
            (session_id,),
        ).fetchone()[0]
        pacing = conn.execute(
            """
            SELECT detail FROM torture_events
            WHERE session_id = ? AND event_type = 'share_pacing_shift'
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    pacing_ms: list[float] = []
    for (detail_raw,) in pacing:
        try:
            d = json.loads(detail_raw or "{}")
            if "pacing_ms" in d:
                pacing_ms.append(float(d["pacing_ms"]))
        except Exception:
            pass
    return {
        "ok": True,
        "session_id": session_id,
        "concurrency_capacity": {
            "orders_ok": int(ok_orders),
            "orders_fail": int(fail_orders),
            "connection_drops": int(drops),
        },
        "trailing_step_modifications": int(trailing),
        "scalping_exit_closures": int(scalp),
        "share_resilience_profile": {
            "min_pacing_ms": min(pacing_ms) if pacing_ms else session_row.get("share_min_pacing_ms"),
            "max_pacing_ms": max(pacing_ms) if pacing_ms else session_row.get("share_max_pacing_ms"),
            "shifts_recorded": len(pacing_ms),
        },
        "session_row": session_row,
    }
