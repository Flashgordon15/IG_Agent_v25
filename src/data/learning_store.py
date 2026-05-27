"""
SQLite learning memory — win/loss tracking and setup performance stats.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Any

from data.models import TradeRecord
from system.closed_trades_display import is_excluded_display_row
from system.ig_transactions import _raw_has_clock_time
from system.pnl_math import classify_result


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _best_pnl_points(row: Any) -> float:
    """
    Return the best available P&L figure in index-points units.

    Preference order:
    1. ig_pnl_currency — already in currency and plausible (|value| <= 5000)
    2. pnl_points — only when small enough to be true index-points (|value| <= 500)
    3. Zero fallback — avoids contaminating averages with Nikkei-scale raw values
    """
    ig = row["ig_pnl_currency"]
    if ig is not None:
        v = float(ig)
        if abs(v) <= 5000:
            return v
    pts = float(row["pnl_points"] or 0)
    if abs(pts) <= 500:
        return pts
    return 0.0


class LearningStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _apply_connection_pragmas(self) -> None:
        assert self._conn is not None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    @_locked
    def connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._apply_connection_pragmas()
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    @_locked
    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _init_schema(self) -> None:
        c = self.conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at TEXT,
                closed_at TEXT,
                market TEXT,
                epic TEXT,
                side TEXT,
                entry REAL,
                exit REAL,
                size REAL,
                stop REAL,
                target REAL,
                pnl_points REAL,
                result TEXT,
                confidence REAL,
                adjusted_confidence REAL,
                setup_key TEXT,
                dry_run INTEGER,
                deal_reference TEXT,
                notes TEXT
            )
            """
        )
        self._migrate_schema(c)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS setup_stats (
                setup_key TEXT PRIMARY KEY,
                trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                breakevens INTEGER DEFAULT 0,
                avg_pnl REAL DEFAULT 0,
                winrate REAL DEFAULT 0,
                last_updated TEXT
            )
            """
        )
        self.conn.commit()

    def _migrate_schema(self, c: sqlite3.Cursor) -> None:
        cols = {row[1] for row in c.execute("PRAGMA table_info(trades)").fetchall()}
        if "ig_deal_id" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN ig_deal_id TEXT")
        if "unrealized_pnl" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN unrealized_pnl REAL")
        if "ig_pnl_currency" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN ig_pnl_currency REAL")
        if "source" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN source TEXT DEFAULT 'strategy'")
        if "ig_close_deal_id" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN ig_close_deal_id TEXT")
        if "confidence_band" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN confidence_band TEXT")
        if "entry_atr" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN entry_atr REAL")
        if "trail_distance" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN trail_distance REAL")
        if "partial_close_done" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN partial_close_done INTEGER DEFAULT 0")
        # Cooldowns table — survives restarts
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                epic TEXT PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    @_locked
    def open_trade(self, record: TradeRecord) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO trades(
                opened_at, market, epic, side, entry, size, stop, target,
                confidence, adjusted_confidence, setup_key, dry_run,
                deal_reference, ig_deal_id, notes
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                record.market,
                record.epic,
                record.side,
                record.entry,
                record.size,
                record.stop,
                record.target,
                record.confidence,
                record.adjusted_confidence,
                record.setup_key,
                int(record.dry_run),
                record.deal_reference,
                (record.extra or {}).get("ig_deal_id") if record.extra else None,
                record.notes,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @_locked
    def get_stop(self, trade_id: int) -> float | None:
        row = self.conn.execute("SELECT stop FROM trades WHERE id=?", (trade_id,)).fetchone()
        return float(row["stop"]) if row else None

    @_locked
    def update_stop(self, trade_id: int, stop: float, note: str) -> None:
        self.conn.execute(
            "UPDATE trades SET stop=?, notes=COALESCE(notes,'') || ? WHERE id=?",
            (stop, note, trade_id),
        )
        self.conn.commit()

    @_locked
    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl_points: float,
        result: str,
        notes: str = "",
        *,
        ig_pnl_currency: float | None = None,
        ig_close_deal_id: str | None = None,
    ) -> None:
        row = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row or row["closed_at"]:
            return
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        close_sets = [
            "closed_at=?",
            "exit=?",
            "pnl_points=?",
            "result=?",
        ]
        close_params: list[Any] = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            exit_price,
            pnl_points,
            result,
        ]
        if "ig_pnl_currency" in cols:
            close_sets.append("ig_pnl_currency=?")
            close_params.append(ig_pnl_currency)
        if "ig_close_deal_id" in cols and ig_close_deal_id:
            close_sets.append("ig_close_deal_id=?")
            close_params.append(str(ig_close_deal_id))
        close_sets.append("notes=COALESCE(notes,'') || ?")
        close_params.append(" | " + notes if notes else "")
        close_params.append(trade_id)
        if "ig_pnl_currency" in cols:
            self.conn.execute(
                f"""
                UPDATE trades
                SET {', '.join(close_sets)}
                WHERE id=?
                """,
                tuple(close_params),
            )
        else:
            self.conn.execute(
                """
                UPDATE trades
                SET closed_at=?, exit=?, pnl_points=?, result=?,
                    notes=COALESCE(notes,'') || ?
                WHERE id=?
                """,
                (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    exit_price,
                    pnl_points,
                    result,
                    " | " + notes if notes else "",
                    trade_id,
                ),
            )
        self.conn.commit()
        self._rebuild_stats_for(row["setup_key"])
        try:
            row_keys = row.keys() if hasattr(row, "keys") else []
            row_epic = str(row["epic"]) if "epic" in row_keys else ""
            row_dry_run = (
                int(row["dry_run"]) if "dry_run" in row_keys and row["dry_run"] is not None else 0
            )
            if row_epic and not row_dry_run:
                from execution.japan225_daily_risk import record_trade_closed

                pnl_value = (
                    float(ig_pnl_currency)
                    if ig_pnl_currency is not None
                    else float(pnl_points or 0.0)
                )
                record_trade_closed(row_epic, pnl=pnl_value, result=str(result or ""))
        except Exception:
            pass

    @_locked
    def ingest_ig_closed_transaction(self, row: dict[str, Any]) -> bool:
        """Upsert a closed trade row from IG transaction history (match IG History UI)."""
        ref = str(row.get("deal_reference") or row.get("ig_deal_id") or "").strip()
        if not ref:
            return False
        ig_pnl = float(row.get("ig_pnl_currency") if row.get("ig_pnl_currency") is not None else row.get("pnl_points") or 0)
        result = str(row.get("result") or classify_result(ig_pnl))
        closed_at = str(row.get("closed_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}

        suffix = ref.upper()
        existing = self.conn.execute(
            """
            SELECT id, closed_at FROM trades
            WHERE ig_deal_id=? OR deal_reference=?
               OR (ig_deal_id IS NOT NULL AND LENGTH(?) >= 6 AND UPPER(ig_deal_id) LIKE ?)
            LIMIT 1
            """,
            (ref, ref, suffix, f"%{suffix}"),
        ).fetchone()

        if existing:
            existing_ts = str(existing["closed_at"] or "")
            if _raw_has_clock_time(existing_ts) and not _raw_has_clock_time(closed_at):
                closed_at = existing_ts
            # Only update ig_pnl_currency (and metadata) — never overwrite pnl_points
            # with a currency value on an existing strategy row (pnl_points stores index
            # points per unit; mixing units corrupts stats and win-rate calculations).
            sets = [
                "closed_at=?",
                "result=?",
            ]
            params: list[Any] = [
                closed_at,
                result,
            ]
            exit_val = float(row.get("exit") or 0)
            if exit_val:
                sets.append("exit=?")
                params.append(exit_val)
            entry_val = float(row.get("entry") or 0)
            if entry_val:
                sets.append("entry=?")
                params.append(entry_val)
            size_val = float(row.get("size") or 0)
            if size_val:
                sets.append("size=?")
                params.append(size_val)
            if "ig_pnl_currency" in cols:
                sets.append("ig_pnl_currency=?")
                params.append(ig_pnl)
            if "ig_deal_id" in cols:
                sets.append("ig_deal_id=?")
                params.append(ref)
            params.append(int(existing["id"]))
            self.conn.execute(
                f"UPDATE trades SET {', '.join(sets)} WHERE id=?",
                params,
            )
            self.conn.commit()
            return True

        market = str(row.get("market") or "")
        epic = str(row.get("epic") or "")
        side = str(row.get("side") or "BUY")
        entry = float(row.get("entry") or 0)
        exit_px = float(row.get("exit") or 0)
        size = float(row.get("size") or 1)
        notes = str(row.get("notes") or "IG transaction history")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        source_col = "'ig_import'" if "source" in cols else None
        if "ig_pnl_currency" in cols and "ig_deal_id" in cols:
            src_clause = ", source" if "source" in cols else ""
            src_val = ", 'ig_import'" if "source" in cols else ""
            self.conn.execute(
                f"""
                INSERT INTO trades(
                    opened_at, closed_at, market, epic, side, entry, exit, size,
                    stop, target, pnl_points, result, confidence, adjusted_confidence,
                    setup_key, dry_run, deal_reference, notes, ig_deal_id, ig_pnl_currency{src_clause}
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{src_val})
                """,
                (
                    closed_at, closed_at, market, epic, side, entry, exit_px, size,
                    0.0, 0.0, ig_pnl, result, 0.0, 0.0, "IG|imported", 0, ref, notes, ref, ig_pnl,
                ),
            )
        else:
            src_clause = ", source" if "source" in cols else ""
            src_val = ", 'ig_import'" if "source" in cols else ""
            self.conn.execute(
                f"""
                INSERT INTO trades(
                    opened_at, closed_at, market, epic, side, entry, exit, size,
                    stop, target, pnl_points, result, confidence, adjusted_confidence,
                    setup_key, dry_run, deal_reference, notes{src_clause}
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{src_val})
                """,
                (
                    closed_at, closed_at, market, epic, side, entry, exit_px, size,
                    0.0, 0.0, ig_pnl, result, 0.0, 0.0, "IG|imported", 0, ref, notes,
                ),
            )
        self.conn.commit()
        return True

    @_locked
    def apply_ig_transaction_pnl(
        self,
        deal_reference: str,
        deal_id: str,
        ig_pnl: float,
        result: str,
        *,
        ig_close_deal_id: str | None = None,
    ) -> bool:
        """Update closed trade P&L from IG transaction history."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "ig_pnl_currency" not in cols:
            return False
        row = None
        keys = [k for k in (deal_id, deal_reference, ig_close_deal_id) if k]
        for key in keys:
            suffix = str(key).upper()
            row = self.conn.execute(
                """
                SELECT id FROM trades
                WHERE ig_deal_id=? OR deal_reference=? OR ig_close_deal_id=?
                   OR (ig_deal_id IS NOT NULL AND LENGTH(?) >= 6 AND UPPER(ig_deal_id) LIKE ?)
                LIMIT 1
                """,
                (key, key, key, suffix, f"%{suffix}"),
            ).fetchone()
            if row:
                break
        if not row:
            return False
        sets = ["ig_pnl_currency=?", "result=?"]
        params: list[Any] = [ig_pnl, result]
        close_id = str(ig_close_deal_id or deal_id or deal_reference or "").strip()
        if "ig_close_deal_id" in cols and close_id:
            sets.append("ig_close_deal_id=?")
            params.append(close_id)
        params.append(row["id"])
        self.conn.execute(
            f"""
            UPDATE trades SET {', '.join(sets)}
            WHERE id=? AND closed_at IS NOT NULL
            """,
            tuple(params),
        )
        self.conn.commit()
        ok = self.conn.total_changes > 0
        if ok:
            try:
                from execution.ml_training_hooks import record_ml_exit_for_deal

                closed = self.conn.execute(
                    """
                    SELECT ig_deal_id, exit_price, pnl_points, result
                    FROM trades WHERE id=?
                    """,
                    (row["id"],),
                ).fetchone()
                exit_px = float(closed["exit_price"] or 0) if closed else 0.0
                pts = float(closed["pnl_points"] or 0) if closed else 0.0
                record_ml_exit_for_deal(
                    str(deal_id or deal_reference),
                    ig_pnl=float(ig_pnl),
                    result=str(result),
                    exit_price=exit_px,
                    pts_pnl=pts,
                    exit_reason="ig_transaction_sync",
                )
            except Exception as e:
                from system.engine_log import log_engine

                log_engine(
                    f"ml_training_store exit hook failed: {type(e).__name__}: {e}"
                )
        return ok

    def _rebuild_stats_for(self, setup_key: str) -> None:
        # Only count strategy-originated trades so IG-imported rows don't dilute
        # the setup statistics used by the adaptive engine.
        rows = list(
            self.conn.execute(
                """
                SELECT pnl_points, ig_pnl_currency, result FROM trades
                WHERE setup_key=? AND closed_at IS NOT NULL
                  AND (source IS NULL OR source = 'strategy')
                """,
                (setup_key,),
            )
        )
        if not rows:
            return
        n = len(rows)
        wins = sum(1 for r in rows if r["result"] == "WIN")
        losses = sum(1 for r in rows if r["result"] == "LOSS")
        bes = sum(1 for r in rows if r["result"] == "BREAKEVEN")
        avg = sum(_best_pnl_points(r) for r in rows) / n
        wr = wins / n
        self.conn.execute(
            """
            INSERT INTO setup_stats(
                setup_key, trades, wins, losses, breakevens,
                avg_pnl, winrate, last_updated
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(setup_key) DO UPDATE SET
                trades=excluded.trades,
                wins=excluded.wins,
                losses=excluded.losses,
                breakevens=excluded.breakevens,
                avg_pnl=excluded.avg_pnl,
                winrate=excluded.winrate,
                last_updated=excluded.last_updated
            """,
            (
                setup_key, n, wins, losses, bes, avg, wr,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.conn.commit()

    @_locked
    def active_trades(self, epic: str | None = None) -> list[sqlite3.Row]:
        if epic:
            return list(
                self.conn.execute(
                    "SELECT * FROM trades WHERE closed_at IS NULL AND epic=? ORDER BY opened_at",
                    (epic,),
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at"
            )
        )

    @_locked
    def has_open_trade(self, epic: str) -> bool:
        return self.count_open_trades(epic) > 0

    @_locked
    def count_open_trades(self, epic: str | None = None) -> int:
        if epic:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE epic=? AND closed_at IS NULL",
                (epic,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE closed_at IS NULL",
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _prefer_execution_row(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
        if not rows:
            return None
        for row in rows:
            if str(row["setup_key"] or "") != "IG_IMPORT":
                return row
        return rows[0]

    @_locked
    def find_open_by_deal_id(self, deal_id: str) -> sqlite3.Row | None:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE ig_deal_id=? AND closed_at IS NULL ORDER BY id DESC",
            (deal_id,),
        ).fetchall()
        return self._prefer_execution_row(rows)

    @_locked
    def find_open_by_deal_reference(self, deal_reference: str) -> sqlite3.Row | None:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE deal_reference=? AND closed_at IS NULL ORDER BY id DESC",
            (deal_reference,),
        ).fetchall()
        return self._prefer_execution_row(rows)

    @_locked
    def set_ig_deal_id(self, trade_id: int, deal_id: str) -> None:
        self.conn.execute(
            "UPDATE trades SET ig_deal_id=? WHERE id=?",
            (deal_id, trade_id),
        )
        self.conn.commit()

    @_locked
    @_locked
    def set_v25_entry_meta(
        self,
        trade_id: int,
        *,
        confidence_band: str,
        entry_atr: float,
        trail_distance: float,
    ) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "confidence_band" not in cols:
            return
        self.conn.execute(
            """
            UPDATE trades
            SET confidence_band=?, entry_atr=?, trail_distance=?
            WHERE id=?
            """,
            (str(confidence_band), float(entry_atr), float(trail_distance), trade_id),
        )
        self.conn.commit()

    @_locked
    def mark_partial_close_done(self, trade_id: int) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "partial_close_done" not in cols:
            return
        self.conn.execute(
            "UPDATE trades SET partial_close_done=1 WHERE id=?",
            (trade_id,),
        )
        self.conn.commit()

    @_locked
    def is_partial_close_done(self, trade_id: int) -> bool:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "partial_close_done" not in cols:
            return False
        row = self.conn.execute(
            "SELECT partial_close_done FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        return bool(row and int(row["partial_close_done"] or 0))

    @_locked
    def update_trade_size(self, trade_id: int, size: float) -> None:
        self.conn.execute(
            "UPDATE trades SET size=?, notes=COALESCE(notes,'') || ? WHERE id=?",
            (size, f" | IG sync size={size}", trade_id),
        )
        self.conn.commit()

    @_locked
    def update_trade_upl(self, trade_id: int, upl: float, level: float | None = None) -> None:
        note = f" | IG upl={upl:.2f}"
        if level is not None:
            note += f" level={level:.1f}"
        self.conn.execute(
            "UPDATE trades SET unrealized_pnl=?, notes=COALESCE(notes,'') || ? WHERE id=?",
            (upl, note, trade_id),
        )
        self.conn.commit()

    @_locked
    def get_last_closed_trade(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    @_locked
    def update_protection_from_execution(
        self,
        trade_id: int,
        *,
        stop: float,
        target: float,
        setup_key: str,
        raw_confidence: float,
        adjusted_confidence: float,
        notes: str,
        deal_reference: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trades
            SET stop=?, target=?, setup_key=?, confidence=?, adjusted_confidence=?,
                deal_reference=COALESCE(?, deal_reference),
                notes=COALESCE(notes,'') || ?
            WHERE id=?
            """,
            (
                stop,
                target,
                setup_key,
                raw_confidence,
                adjusted_confidence,
                deal_reference or None,
                notes,
                trade_id,
            ),
        )
        self.conn.commit()

    @_locked
    def import_ig_position(
        self,
        *,
        epic: str,
        market: str,
        side: str,
        entry: float,
        size: float,
        deal_id: str,
        deal_reference: str = "",
        notes: str = "",
        stop_level: float = 0.0,
        limit_level: float = 0.0,
    ) -> int:
        stop = float(stop_level) if float(stop_level) > 0 else float(entry)
        target = float(limit_level) if float(limit_level) > 0 else float(entry)
        cur = self.conn.cursor()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        src_clause = ", source" if "source" in cols else ""
        src_val = ", 'ig_import'" if "source" in cols else ""
        cur.execute(
            f"""
            INSERT INTO trades(
                opened_at, market, epic, side, entry, size, stop, target,
                confidence, adjusted_confidence, setup_key, dry_run,
                deal_reference, ig_deal_id, notes{src_clause}
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{src_val})
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                market, epic, side, entry, size, stop, target,
                0.0, 0.0, "IG_IMPORT", 0, deal_reference or None, deal_id, notes,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @_locked
    def setup_stats(self, setup_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM setup_stats WHERE setup_key=?", (setup_key,)
        ).fetchone()
        return dict(row) if row else None

    def _realised_pnl_expr(self) -> str:
        """
        Sum only plausible currency P&L — never treat index points (e.g. 61151) as GBP/USD.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "ig_pnl_currency" in cols:
            return """
                CASE
                    WHEN ig_pnl_currency IS NOT NULL AND ABS(ig_pnl_currency) <= 5000
                        THEN ig_pnl_currency
                    WHEN ABS(COALESCE(pnl_points, 0)) <= 150
                        THEN COALESCE(pnl_points, 0) * COALESCE(size, 1)
                    ELSE 0
                END
            """
        return """
            CASE
                WHEN ABS(COALESCE(pnl_points, 0)) <= 150
                    THEN COALESCE(pnl_points, 0) * COALESCE(size, 1)
                ELSE 0
            END
        """

    @_locked
    def sum_closed_pnl(self) -> float:
        expr = self._realised_pnl_expr()
        row = self.conn.execute(
            f"SELECT COALESCE(SUM({expr}), 0) AS s FROM trades WHERE closed_at IS NOT NULL"
        ).fetchone()
        return float(row["s"] or 0) if row else 0.0

    @_locked
    def sum_daily_pnl(self, day: str | None = None) -> float:
        """Sum realised P&L (currency) for trades closed on day (YYYY-MM-DD); default today."""
        d = day or date.today().strftime("%Y-%m-%d")
        expr = self._realised_pnl_expr()
        row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM({expr}), 0) AS s
            FROM trades
            WHERE closed_at IS NOT NULL AND substr(closed_at, 1, 10) = ?
            """,
            (d,),
        ).fetchone()
        return float(row["s"] or 0) if row else 0.0

    @_locked
    def count_trades_opened_today(self, day: str | None = None) -> int:
        """Count non-dry-run trades opened on day (YYYY-MM-DD); default today."""
        d = day or date.today().strftime("%Y-%m-%d")
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM trades
            WHERE substr(opened_at, 1, 10) = ? AND dry_run = 0
            """,
            (d,),
        ).fetchone()
        return int(row["n"]) if row else 0

    @_locked
    def sum_open_risk_points(self) -> float:
        """Approximate open risk as sum(size * |entry - stop|) for open trades."""
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(size, 1) * ABS(entry - stop)), 0) AS s
            FROM trades
            WHERE closed_at IS NULL
            """
        ).fetchone()
        return float(row["s"] or 0) if row else 0.0

    @_locked
    def sum_open_unrealized_pnl(self) -> float:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "unrealized_pnl" not in cols:
            return 0.0
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(unrealized_pnl), 0) AS s
            FROM trades WHERE closed_at IS NULL
            """
        ).fetchone()
        return float(row["s"] or 0) if row else 0.0

    @_locked
    def rolling_stats(self, n: int = 20) -> dict[str, Any]:
        """Win-rate stats for the most recent n closed strategy trades."""
        rows = list(
            self.conn.execute(
                """
                SELECT pnl_points, ig_pnl_currency, result FROM trades
                WHERE closed_at IS NOT NULL AND dry_run = 0
                  AND (source IS NULL OR source = 'strategy')
                ORDER BY closed_at DESC LIMIT ?
                """,
                (n,),
            )
        )
        if not rows:
            return {"trades": 0, "wins": 0, "losses": 0, "winrate": 0.0, "avg_pnl": 0.0}
        total = len(rows)
        wins = sum(1 for r in rows if r["result"] == "WIN")
        losses = sum(1 for r in rows if r["result"] == "LOSS")
        return {
            "trades": total,
            "wins": wins,
            "losses": losses,
            "winrate": wins / total,
            "avg_pnl": sum(_best_pnl_points(r) for r in rows) / total,
        }

    @_locked
    def global_stats(self) -> dict[str, Any]:
        """All-time stats for strategy trades only (excludes IG-imported rows)."""
        rows = list(
            self.conn.execute(
                """
                SELECT pnl_points, ig_pnl_currency, result FROM trades
                WHERE closed_at IS NOT NULL
                  AND (source IS NULL OR source = 'strategy')
                """
            )
        )
        if not rows:
            return {"trades": 0, "wins": 0, "losses": 0, "winrate": 0.0, "avg_pnl": 0.0}
        n = len(rows)
        wins = sum(1 for r in rows if r["result"] == "WIN")
        losses = sum(1 for r in rows if r["result"] == "LOSS")
        return {
            "trades": n,
            "wins": wins,
            "losses": losses,
            "winrate": wins / n,
            "avg_pnl": sum(_best_pnl_points(r) for r in rows) / n,
        }

    @_locked
    def normalize_deal_references(self) -> int:
        """Remove duplicate TYNK confirm rows when IG transaction ingest has the close."""
        cur = self.conn.cursor()
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (deal_reference LIKE '%TYNK' OR deal_reference LIKE '%TYPT')
              AND ig_deal_id IS NOT NULL
              AND TRIM(ig_deal_id) != ''
            """
        )
        removed = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (deal_reference LIKE '%TYNK' OR deal_reference LIKE '%TYPT')
              AND (ig_deal_id IS NULL OR TRIM(ig_deal_id) = '')
            """
        )
        removed += cur.rowcount
        self.conn.commit()
        return removed

    @_locked
    def purge_non_broker_history(self, *, keep_days: int = 7) -> dict[str, int]:
        """
        Remove TEST/simulator rows and closes without IG deal IDs (keeps IG-synced rows).
        """
        cutoff = (datetime.now() - timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.cursor()
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND deal_reference LIKE 'SIM-%'
            """
        )
        removed_sim_prefix = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND dry_run = 1
            """
        )
        removed_dry = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (deal_reference LIKE '%TYNK' OR deal_reference LIKE '%TYPT')
              AND (notes NOT LIKE '%IG transaction%')
            """
        )
        removed_confirm = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (ig_deal_id IS NULL OR TRIM(ig_deal_id) = '')
              AND (
                notes LIKE '%simulator%'
                OR notes LIKE '%e2e%'
                OR setup_key LIKE '%|e2e%'
                OR deal_reference LIKE 'W%'
                OR deal_reference LIKE 'L%'
                OR notes LIKE '%TEST%'
              )
            """
        )
        removed_sim = cur.rowcount + removed_dry + removed_confirm + removed_sim_prefix
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (ig_deal_id IS NULL OR TRIM(ig_deal_id) = '')
              AND (notes NOT LIKE '%IG sync%' AND notes NOT LIKE '%IG transaction%')
              AND closed_at < ?
            """,
            (cutoff,),
        )
        removed_old = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (ig_deal_id IS NULL OR TRIM(ig_deal_id) = '')
              AND (notes NOT LIKE '%IG sync%' AND notes NOT LIKE '%IG transaction%')
              AND (deal_reference IS NULL OR deal_reference NOT LIKE 'DIAAA%')
            """
        )
        removed_orphan = cur.rowcount
        cur.execute(
            """
            DELETE FROM trades
            WHERE closed_at IS NOT NULL
              AND (ig_deal_id IS NULL OR TRIM(ig_deal_id) = '')
              AND ABS(COALESCE(pnl_points, 0)) > 500
            """
        )
        removed_bogus = cur.rowcount
        self.conn.commit()
        cur.execute("DELETE FROM setup_stats")
        rows = self.conn.execute(
            "SELECT DISTINCT setup_key FROM trades WHERE closed_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            self._rebuild_stats_for(row["setup_key"])
        return {
            "removed_simulator": removed_sim,
            "removed_old_local": removed_old,
            "removed_orphan_local": removed_orphan,
            "removed_bogus_pnl": removed_bogus,
        }

    @_locked
    def count_closed_trades(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE closed_at IS NOT NULL"
        ).fetchone()
        return int(row["n"]) if row else 0

    @_locked
    def recent_closed_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM trades
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def recent_confirmed_closed_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        """Last *limit* IG-confirmed closes (ig_pnl_currency set), excluding SIM/soak rows."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "ig_pnl_currency" not in cols:
            return []
        rows = self.conn.execute(
            """
            SELECT *,
                   COALESCE(ig_pnl_currency, pnl_points, 0) AS pnl
            FROM trades
            WHERE closed_at IS NOT NULL
              AND ig_pnl_currency IS NOT NULL
              AND dry_run = 0
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            if is_excluded_display_row(d):
                continue
            d["pnl"] = _best_pnl_points(row)
            out.append(d)
        return out

    def is_writable(self) -> bool:
        import os
        from pathlib import Path
        p = Path(self.db_path)
        parent = p.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
        if p.exists():
            return os.access(p, os.W_OK)
        return os.access(parent, os.W_OK)

    # ------------------------------------------------------------------
    # Cooldown persistence (survive restarts)
    # ------------------------------------------------------------------

    @_locked
    def record_cooldown(self, epic: str, cooldown_seconds: int) -> None:
        """Persist a cooldown for *epic* that expires after *cooldown_seconds*."""
        now = datetime.now()
        expires = now + timedelta(seconds=cooldown_seconds)
        self.conn.execute(
            """
            INSERT INTO cooldowns(epic, recorded_at, expires_at)
            VALUES(?, ?, ?)
            ON CONFLICT(epic) DO UPDATE SET
                recorded_at = excluded.recorded_at,
                expires_at  = excluded.expires_at
            """,
            (
                epic,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                expires.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.conn.commit()

    @_locked
    def load_active_cooldowns(self) -> dict[str, datetime]:
        """Return {epic: recorded_at datetime} for all unexpired cooldown rows."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT epic, recorded_at FROM cooldowns WHERE expires_at > ?",
            (now_str,),
        ).fetchall()
        result: dict[str, datetime] = {}
        for row in rows:
            try:
                result[row["epic"]] = datetime.strptime(
                    str(row["recorded_at"]), "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                pass
        # Prune expired rows while we're here
        self.conn.execute("DELETE FROM cooldowns WHERE expires_at <= ?", (now_str,))
        self.conn.commit()
        return result

    @_locked
    def clear_cooldown(self, epic: str) -> None:
        self.conn.execute("DELETE FROM cooldowns WHERE epic=?", (epic,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # Consecutive-loss circuit breaker
    # ------------------------------------------------------------------

    @_locked
    def consecutive_losses(self, n: int = 5) -> int:
        """Count trailing consecutive LOSS results in the last *n* strategy trades."""
        rows = list(
            self.conn.execute(
                """
                SELECT result FROM trades
                WHERE closed_at IS NOT NULL
                  AND dry_run = 0
                  AND (source IS NULL OR source = 'strategy')
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (n,),
            )
        )
        count = 0
        for row in rows:
            if str(row["result"]) == "LOSS":
                count += 1
            else:
                break
        return count

    @_locked
    def get_runtime_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM runtime_state WHERE key=?",
            (str(key),),
        ).fetchone()
        return str(row["value"]) if row else None

    @_locked
    def set_runtime_state(self, key: str, value: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(key), str(value), now),
        )
        self.conn.commit()

    @_locked
    def clear_runtime_state(self, key: str) -> None:
        self.conn.execute("DELETE FROM runtime_state WHERE key=?", (str(key),))
        self.conn.commit()

    @_locked
    def clear_circuit_breaker_state(self) -> None:
        self.conn.execute(
            "DELETE FROM runtime_state WHERE key IN ('circuit_breaker_tripped_at', 'circuit_breaker_half_size')"
        )
        self.conn.commit()

    @_locked
    def circuit_breaker_half_size_active(self) -> bool:
        return self.get_runtime_state("circuit_breaker_half_size") == "1"
