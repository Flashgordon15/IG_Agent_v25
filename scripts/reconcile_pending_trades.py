#!/usr/bin/env python3
"""
One-time offline reconciliation for closed trades awaiting IG P&L confirm.

Opens learning_db.sqlite3 directly (WAL), fetches IG transaction history via REST,
and updates rows without touching the running GUI process.

Usage:
  PYTHONPATH=src python3 scripts/reconcile_pending_trades.py
  PYTHONPATH=src python3 scripts/reconcile_pending_trades.py --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

def _log(msg: str) -> None:
    print(msg, flush=True)


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _select_close_deal_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows with local close ref but no confirmed IG P&L (user spec)."""
    return list(
        conn.execute(
            """
            SELECT id, ig_deal_id, ig_close_deal_id, deal_reference, closed_at, result, epic
            FROM trades
            WHERE ig_pnl_currency IS NULL
              AND ig_close_deal_id IS NOT NULL
              AND TRIM(ig_close_deal_id) != ''
              AND closed_at IS NOT NULL
              AND TRIM(closed_at) != ''
              AND datetime(closed_at) < datetime('now', '-2 hours')
            ORDER BY closed_at DESC
            """
        ).fetchall()
    )


def _select_open_deal_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Broker closes with open deal id only — display 'Pending IG confirm' rows."""
    return list(
        conn.execute(
            """
            SELECT id, ig_deal_id, ig_close_deal_id, deal_reference, closed_at, result, epic
            FROM trades
            WHERE ig_pnl_currency IS NULL
              AND ig_deal_id IS NOT NULL
              AND TRIM(ig_deal_id) != ''
              AND (ig_close_deal_id IS NULL OR TRIM(ig_close_deal_id) = '')
              AND closed_at IS NOT NULL
              AND TRIM(closed_at) != ''
              AND datetime(closed_at) < datetime('now', '-2 hours')
              AND LOWER(COALESCE(source, 'strategy')) IN ('strategy', 'ig_import', 'ig_sync')
              AND UPPER(ig_deal_id) NOT LIKE 'SIM-%'
            ORDER BY closed_at DESC
            """
        ).fetchall()
    )


def _build_ig_index(ig_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from system.ig_transactions import ig_deal_key_variants

    index: dict[str, dict[str, Any]] = {}
    for row in ig_rows:
        keys: set[str] = set()
        for field in ("ig_deal_id", "deal_reference"):
            keys.update(ig_deal_key_variants(str(row.get(field) or "")))
        for key in keys:
            if key:
                index[key.upper()] = row
    return index


def _lookup_ig_row(
    index: dict[str, dict[str, Any]],
    *refs: str,
) -> dict[str, Any] | None:
    from system.ig_transactions import ig_deal_key_variants

    for ref in refs:
        for key in ig_deal_key_variants(ref):
            hit = index.get(key.upper())
            if hit:
                return hit
    return None


def _heuristic_match(
    local: sqlite3.Row,
    ig_rows: list[dict[str, Any]],
    claimed: set[str],
) -> dict[str, Any] | None:
    from runtime.ig_transaction_sync import IgTransactionSync

    local_dict = dict(local)
    return IgTransactionSync._heuristic_match(local_dict, ig_rows, claimed)


def _parse_closed_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            part = text[:19] if " " in fmt else text[:10]
            return datetime.strptime(part, fmt)
        except ValueError:
            continue
    return None


def _mark_unconfirmed(conn: sqlite3.Connection, row_id: int, *, dry_run: bool) -> bool:
    if dry_run:
        return True
    conn.execute(
        "UPDATE trades SET result=? WHERE id=? AND ig_pnl_currency IS NULL",
        ("Unconfirmed", row_id),
    )
    conn.commit()
    return conn.total_changes > 0


def _apply_reconcile(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    ig_row: dict[str, Any],
    *,
    dry_run: bool,
    sym: str,
) -> bool:
    open_id = str(row["ig_deal_id"] or row["deal_reference"] or "").strip()
    close_ref = str(
        ig_row.get("ig_deal_id") or ig_row.get("deal_reference") or row["ig_close_deal_id"] or ""
    ).strip()
    pnl = float(ig_row.get("ig_pnl_currency") or 0)
    result = str(ig_row.get("result") or "").strip() or "UNKNOWN"

    if dry_run:
        _log(
            f"Reconciled: open={open_id[:16] or '—'} close={close_ref[:16]} "
            f"P&L={sym}{pnl:+.2f} result={result} (dry-run)"
        )
        return True

    conn.execute(
        """
        UPDATE trades
        SET ig_pnl_currency=?, result=?, ig_close_deal_id=COALESCE(NULLIF(TRIM(ig_close_deal_id), ''), ?)
        WHERE id=? AND ig_pnl_currency IS NULL
        """,
        (pnl, result, close_ref, row["id"]),
    )
    conn.commit()
    if conn.total_changes > 0:
        _log(
            f"Reconciled: open={open_id[:16] or '—'} close={close_ref[:16]} "
            f"P&L={sym}{pnl:+.2f} result={result}"
        )
        return True
    return False


def _fetch_ig_rows(*, epic: str, history_days: int) -> list[dict[str, Any]]:
    from ig_api.rest_client import IGRestClient
    from system.closed_trades_reconcile import fetch_ig_closed_rows
    from system.credentials_holder import bootstrap_credentials, get_credentials_holder
    from system.ig_rest_sync_lock import ig_rest_sync_lock

    bootstrap_credentials()
    status = get_credentials_holder().reload()
    if not status.loaded or not status.credentials:
        raise RuntimeError("Credentials not loaded — check config/credentials/credentials.json")

    client = IGRestClient(status.credentials)
    with ig_rest_sync_lock():
        client.login()
        return fetch_ig_closed_rows(
            client,
            epic=epic,
            history_days=history_days,
            display_hours=max(168.0, history_days * 24.0),
        )


def _dedupe_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[int] = set()
    out: list[sqlite3.Row] = []
    for row in rows:
        rid = int(row["id"])
        if rid in seen:
            continue
        seen.add(rid)
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile pending IG P&L in learning_db")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no DB writes")
    parser.add_argument("--history-days", type=int, default=7, help="IG history window (days)")
    args = parser.parse_args()

    from system.account_currency import account_currency_symbol
    from system.config_loader import get_config

    cfg = get_config(reload=True)
    db_path = ROOT / cfg.learning_db
    if not db_path.is_file():
        _log(f"Database not found: {db_path}")
        return 1

    conn = _open_db(db_path)
    close_pending = _select_close_deal_pending(conn)
    open_pending = _select_open_deal_pending(conn)
    pending = _dedupe_rows(close_pending + open_pending)

    _log(f"Pending rows (close-ref): {len(close_pending)}")
    _log(f"Pending rows (open-deal):  {len(open_pending)}")
    _log(f"Total unique pending:      {len(pending)}")

    if not pending:
        _log("Nothing to reconcile.")
        conn.close()
        return 0

    sym = account_currency_symbol()
    try:
        ig_rows = _fetch_ig_rows(epic=cfg.epic, history_days=args.history_days)
    except Exception as exc:
        _log(f"IG fetch failed: {type(exc).__name__}: {exc}")
        conn.close()
        return 1

    _log(f"IG transaction rows fetched: {len(ig_rows)}")
    index = _build_ig_index(ig_rows)
    claimed: set[str] = set()
    reconciled = 0
    unconfirmed = 0

    for row in pending:
        open_id = str(row["ig_deal_id"] or row["deal_reference"] or "")
        close_id = str(row["ig_close_deal_id"] or "")
        ig_row = _lookup_ig_row(index, close_id, open_id)
        if ig_row is None and open_id:
            ig_row = _heuristic_match(row, ig_rows, claimed)
        if ig_row:
            close_ref = str(ig_row.get("ig_deal_id") or ig_row.get("deal_reference") or "").upper()
            if close_ref:
                claimed.add(close_ref)
            if _apply_reconcile(conn, row, ig_row, dry_run=args.dry_run, sym=sym):
                reconciled += 1
            continue

        closed_dt = _parse_closed_ts(str(row["closed_at"] or ""))
        age_ok = closed_dt and (datetime.now() - closed_dt) >= timedelta(hours=2)
        if age_ok:
            if args.dry_run:
                _log(f"Unconfirmed (dry-run): id={row['id']} open={open_id[:16]} close={close_id[:16]}")
            elif _mark_unconfirmed(conn, int(row["id"]), dry_run=args.dry_run):
                _log(f"Unconfirmed: id={row['id']} open={open_id[:16]} close={close_id[:16] or '—'}")
            unconfirmed += 1

    remain = conn.execute(
        """
        SELECT COUNT(*) FROM trades
        WHERE ig_pnl_currency IS NULL
          AND ig_deal_id IS NOT NULL AND TRIM(ig_deal_id) != ''
          AND closed_at IS NOT NULL AND TRIM(closed_at) != ''
          AND datetime(closed_at) < datetime('now', '-2 hours')
          AND LOWER(COALESCE(source, 'strategy')) IN ('strategy', 'ig_import', 'ig_sync')
          AND UPPER(ig_deal_id) NOT LIKE 'SIM-%'
        """
    ).fetchone()[0]

    _log("")
    _log(f"Reconciled:   {reconciled}")
    _log(f"Unconfirmed:  {unconfirmed}")
    _log(f"Still pending (null ig_pnl, broker, >2h): {remain}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
