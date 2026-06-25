"""
Resilient triage SQLite connections — v31.1 ledger write pool.

All triage / torture ledger writers share ``busy_timeout=5000`` so concurrent
async bursts wait for locks instead of crashing mid-soak.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

TRIAGE_BUSY_TIMEOUT_MS = 5000
TRIAGE_CONNECT_TIMEOUT_SEC = TRIAGE_BUSY_TIMEOUT_MS / 1000.0


def configure_sqlite_connection(conn: sqlite3.Connection, *, wal: bool = True) -> None:
    """Apply standard pragmas for concurrent async writers."""
    conn.execute(f"PRAGMA busy_timeout={TRIAGE_BUSY_TIMEOUT_MS};")
    if wal:
        conn.execute("PRAGMA journal_mode=WAL;")


def connect_triage_sqlite(
    path: Path | str,
    *,
    row_factory: Any | None = None,
    wal: bool = True,
) -> sqlite3.Connection:
    """Open triage DB with enforced 5000ms busy wait."""
    conn = sqlite3.connect(str(path), timeout=TRIAGE_CONNECT_TIMEOUT_SEC)
    configure_sqlite_connection(conn, wal=wal)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def aiosqlite_connect_kwargs() -> dict[str, Any]:
    """Kwargs + pragma list for aiosqlite workers."""
    return {
        "timeout": TRIAGE_CONNECT_TIMEOUT_SEC,
        "pragmas": [
            ("journal_mode", "WAL"),
            ("busy_timeout", TRIAGE_BUSY_TIMEOUT_MS),
        ],
    }
