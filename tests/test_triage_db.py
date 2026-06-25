"""Triage SQLite connection pool — busy_timeout enforcement."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from analytics.triage_db import TRIAGE_BUSY_TIMEOUT_MS, connect_triage_sqlite


def test_connect_triage_sqlite_sets_busy_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "triage.db"
        conn = connect_triage_sqlite(path)
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            assert int(row[0]) == TRIAGE_BUSY_TIMEOUT_MS
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            assert str(journal[0]).lower() == "wal"
        finally:
            conn.close()
