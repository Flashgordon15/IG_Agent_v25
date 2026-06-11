"""SQLite WAL mode and crash-safety pragmas for LearningStore."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from data.learning_store import LearningStore


def _temp_db() -> tuple[LearningStore, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "learning.db"
    store = LearningStore(str(db_path))
    store.connect()
    return store, db_path, tmp


def test_journal_mode_is_wal() -> None:
    store, _db_path, tmp = _temp_db()
    try:
        row = store.conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal"
        sync = store.conn.execute("PRAGMA synchronous").fetchone()
        assert sync is not None
        assert int(sync[0]) == 1  # NORMAL
    finally:
        store.close()
        tmp.cleanup()


def test_db_integrity_after_aborted_transaction() -> None:
    """Best-effort: committed rows survive an uncommitted write + abrupt close."""
    store, db_path, tmp = _temp_db()
    try:
        store.conn.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES('committed_marker', 'ok', '2026-01-01 00:00:00')
            """
        )
        store.conn.commit()

        crash_conn = sqlite3.connect(db_path, check_same_thread=False)
        crash_conn.execute("PRAGMA journal_mode=WAL")
        crash_conn.execute("BEGIN IMMEDIATE")
        crash_conn.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES('uncommitted_marker', 'lost', '2026-01-01 00:00:00')
            """
        )
        crash_conn.close()  # no commit — simulates crash mid-transaction

        verify = LearningStore(str(db_path))
        verify.connect()
        try:
            integrity = verify.conn.execute("PRAGMA integrity_check").fetchone()
            assert integrity is not None
            assert str(integrity[0]).lower() == "ok"

            row = verify.conn.execute(
                "SELECT value FROM runtime_state WHERE key='committed_marker'"
            ).fetchone()
            assert row is not None
            assert row[0] == "ok"

            ghost = verify.conn.execute(
                "SELECT 1 FROM runtime_state WHERE key='uncommitted_marker'"
            ).fetchone()
            assert ghost is None
        finally:
            verify.close()
    finally:
        store.close()
        tmp.cleanup()
