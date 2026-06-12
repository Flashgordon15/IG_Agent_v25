"""
Administrative read-only exports — shadow training registry audit utility.

Does not mutate learning_db or active trade flows.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_TABLE = "shadow_training_registry"
_DEFAULT_DB = "learning_db.sqlite3"

_CSV_COLUMNS = (
    "opened_at",
    "closed_at",
    "epic",
    "market",
    "side",
    "entry_price",
    "exit_price",
    "pnl_points",
    "ig_pnl_gbp",
    "result",
    "setup_key",
    "source",
    "deal_reference",
    "ingested_at",
    "cumulative_win_rate",
    "epic_cumulative_win_rate",
)


def _default_db_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return data_dir() / _DEFAULT_DB


def _open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"learning database not found: {db_path}")
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _is_win(row: sqlite3.Row) -> bool:
    result = str(row["result"] or "").strip().upper()
    if result in ("WIN", "W"):
        return True
    if result in ("LOSS", "L", "LOSE"):
        return False
    pnl = row["ig_pnl_currency"]
    if pnl is None:
        pnl = row["pnl_points"]
    try:
        return float(pnl or 0) > 0
    except (TypeError, ValueError):
        return False


def _fetch_shadow_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT
            id,
            opened_at,
            closed_at,
            epic,
            market,
            side,
            entry,
            "exit",
            pnl_points,
            ig_pnl_currency,
            result,
            setup_key,
            source,
            deal_reference,
            ig_deal_id,
            ingested_at
        FROM {_TABLE}
        WHERE is_shadow = 1
        ORDER BY closed_at ASC, id ASC
        """
    ).fetchall()


def _annotate_implied_win_rates(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    overall_wins = 0
    overall_total = 0
    epic_wins: dict[str, int] = {}
    epic_totals: dict[str, int] = {}

    annotated: list[dict[str, Any]] = []
    for row in rows:
        epic = str(row["epic"] or "")
        win = _is_win(row)
        overall_total += 1
        epic_totals[epic] = epic_totals.get(epic, 0) + 1
        if win:
            overall_wins += 1
            epic_wins[epic] = epic_wins.get(epic, 0) + 1

        cumulative_wr = round(overall_wins / overall_total, 4) if overall_total else 0.0
        epic_total = epic_totals[epic]
        epic_wr = round(epic_wins.get(epic, 0) / epic_total, 4) if epic_total else 0.0

        annotated.append(
            {
                "opened_at": row["opened_at"] or "",
                "closed_at": row["closed_at"] or "",
                "epic": epic,
                "market": row["market"] or "",
                "side": row["side"] or "",
                "entry_price": row["entry"],
                "exit_price": row["exit"],
                "pnl_points": row["pnl_points"],
                "ig_pnl_gbp": row["ig_pnl_currency"],
                "result": row["result"] or "",
                "setup_key": row["setup_key"] or "",
                "source": row["source"] or "",
                "deal_reference": row["deal_reference"] or row["ig_deal_id"] or "",
                "ingested_at": row["ingested_at"] or "",
                "cumulative_win_rate": cumulative_wr,
                "epic_cumulative_win_rate": epic_wr,
            }
        )
    return annotated


def _rows_to_csv_text(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def export_shadow_registry_to_csv(
    *,
    db_path: Path | str | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Read all shadow_training_registry rows (read-only) and serialize to CSV.

    Returns metadata plus either csv_text (in-memory) or csv_path when written to disk.
    """
    path = _default_db_path(db_path)
    conn = _open_readonly_connection(path)
    try:
        raw_rows = _fetch_shadow_rows(conn)
    finally:
        conn.close()

    annotated = _annotate_implied_win_rates(raw_rows)
    csv_text = _rows_to_csv_text(annotated)

    wins = sum(1 for r in raw_rows if _is_win(r))
    total = len(raw_rows)
    summary = {
        "row_count": total,
        "wins": wins,
        "losses": total - wins,
        "overall_win_rate": round(wins / total, 4) if total else 0.0,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_path": str(path),
    }

    result: dict[str, Any] = {
        "ok": True,
        "summary": summary,
        "csv_text": csv_text,
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(csv_text, encoding="utf-8")
        result["csv_path"] = str(out.resolve())
        log_engine(
            f"data_exporter: shadow registry CSV written ({total} rows) -> {out.name}"
        )
    else:
        log_engine(f"data_exporter: shadow registry CSV prepared ({total} rows)")

    return result
