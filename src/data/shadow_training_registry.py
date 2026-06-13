"""
Isolated shadow training plane — IG-import rows excluded from live learning.

Records carry is_shadow=True and live only in shadow_training_registry (not setup_stats,
expectancy, or ml_training_store.jsonl). Background ML workers may read them for augmentation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from system.learning_trade_policy import is_ig_import_setup_key

_TABLE = "shadow_training_registry"


def ensure_schema(c: sqlite3.Cursor) -> None:
    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ig_deal_id TEXT,
            deal_reference TEXT,
            opened_at TEXT,
            closed_at TEXT,
            market TEXT,
            epic TEXT,
            side TEXT,
            entry REAL,
            exit REAL,
            size REAL,
            pnl_points REAL,
            ig_pnl_currency REAL,
            result TEXT,
            setup_key TEXT,
            source TEXT DEFAULT 'ig_import',
            notes TEXT,
            is_shadow INTEGER NOT NULL DEFAULT 1,
            ingested_at TEXT NOT NULL
        )
        """
    )
    c.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_registry_deal_ref
        ON {_TABLE}(deal_reference)
        WHERE deal_reference IS NOT NULL AND TRIM(deal_reference) != ''
        """
    )


def is_shadow_registry_row(row: dict[str, Any] | Any) -> bool:
    """True when a dict row belongs to the shadow training plane."""
    if hasattr(row, "keys"):
        if "is_shadow" in row.keys():
            return bool(row["is_shadow"])
        setup_key = str(row["setup_key"] if "setup_key" in row.keys() else "")
        source = str(row["source"] if "source" in row.keys() else "")
    else:
        if row.get("is_shadow") is not None:
            return bool(row.get("is_shadow"))
        setup_key = str(row.get("setup_key") or "")
        source = str(row.get("source") or "")
    if is_ig_import_setup_key(setup_key):
        return True
    src = source.lower().strip()
    return src in ("ig_import", "ig|imported", "ig_imported", "ig transaction history")


def _deal_key(row: dict[str, Any]) -> str:
    ref = str(row.get("deal_reference") or row.get("ig_deal_id") or "").strip()
    return ref


def upsert_ig_import(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """
    Insert or update one IG-import closed trade in shadow_training_registry.

    Returns True when a row was written.
    """
    ref = _deal_key(row)
    if not ref:
        return False
    from system.pnl_accounting import normalize_shadow_net_pnl

    row = normalize_shadow_net_pnl(row)
    setup_key = str(row.get("setup_key") or "IG|imported")
    source = str(row.get("source") or "ig_import")
    if not is_shadow_registry_row({"setup_key": setup_key, "source": source}):
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ig_pnl = row.get("ig_pnl_currency")
    if ig_pnl is None:
        ig_pnl = row.get("pnl_points")
    ig_pnl_f = float(ig_pnl or 0)
    result = str(row.get("result") or "")
    closed_at = str(row.get("closed_at") or now)
    opened_at = str(row.get("opened_at") or closed_at)

    cur = conn.cursor()
    existing = cur.execute(
        f"""
        SELECT id FROM {_TABLE}
        WHERE deal_reference=? OR (ig_deal_id IS NOT NULL AND ig_deal_id=?)
        LIMIT 1
        """,
        (ref, ref),
    ).fetchone()

    params = (
        ref,
        ref,
        opened_at,
        closed_at,
        str(row.get("market") or ""),
        str(row.get("epic") or ""),
        str(row.get("side") or ""),
        float(row.get("entry") or 0),
        float(row.get("exit") or 0),
        float(row.get("size") or 1),
        float(row.get("pnl_points") or ig_pnl_f),
        ig_pnl_f,
        result,
        setup_key,
        source,
        str(row.get("notes") or "IG transaction history"),
        1,
        now,
    )

    if existing:
        update_params = params[:-2] + (now,)
        cur.execute(
            f"""
            UPDATE {_TABLE} SET
                ig_deal_id=?,
                deal_reference=?,
                opened_at=?,
                closed_at=?,
                market=?,
                epic=?,
                side=?,
                entry=?,
                exit=?,
                size=?,
                pnl_points=?,
                ig_pnl_currency=?,
                result=?,
                setup_key=?,
                source=?,
                notes=?,
                is_shadow=1,
                ingested_at=?
            WHERE id=?
            """,
            update_params + (int(existing["id"]),),
        )
    else:
        cur.execute(
            f"""
            INSERT INTO {_TABLE}(
                ig_deal_id, deal_reference, opened_at, closed_at, market, epic,
                side, entry, exit, size, pnl_points, ig_pnl_currency, result,
                setup_key, source, notes, is_shadow, ingested_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            params,
        )
    conn.commit()
    return conn.total_changes > 0


def backfill_from_trades(conn: sqlite3.Connection) -> int:
    """One-time copy of existing IG-import trades rows into shadow registry."""
    cur = conn.cursor()
    row = cur.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}").fetchone()
    if row and int(row["n"] or 0) > 0:
        return 0
    rows = cur.execute(
        """
        SELECT * FROM trades
        WHERE closed_at IS NOT NULL
          AND (
            UPPER(COALESCE(setup_key,'')) LIKE 'IG|%'
            OR UPPER(COALESCE(setup_key,'')) IN ('IG_IMPORT', 'IG|IMPORTED')
            OR LOWER(COALESCE(source,'')) IN ('ig_import', 'ig|imported')
          )
        """
    ).fetchall()
    count = 0
    for r in rows:
        if upsert_ig_import(conn, dict(r)):
            count += 1
    return count


def count_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}").fetchone()
    return int(row["n"] or 0) if row else 0


def list_for_ml_training(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rows mapped for build_training_dataset / background ML workers."""
    rows = conn.execute(
        f"""
        SELECT * FROM {_TABLE}
        WHERE is_shadow = 1 AND closed_at IS NOT NULL
        ORDER BY closed_at
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "deal_id": d.get("deal_reference") or d.get("ig_deal_id") or "",
                "instrument": d.get("market") or d.get("epic") or "",
                "epic": d.get("epic") or "",
                "entry_time": d.get("opened_at") or d.get("closed_at") or "",
                "exit_time": d.get("closed_at") or "",
                "entry_price": float(d.get("entry") or 0),
                "exit_price": float(d.get("exit") or 0),
                "gbp_pnl": float(d.get("ig_pnl_currency") or 0),
                "pts_pnl": float(d.get("pnl_points") or 0),
                "result": str(d.get("result") or ""),
                "setup_name": str(d.get("setup_key") or "IG|imported"),
                "source": str(d.get("source") or "ig_import"),
                "is_shadow": True,
                "confirmed": True,
                "version": "shadow_registry",
            }
        )
    return out
