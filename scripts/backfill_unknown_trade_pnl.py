#!/usr/bin/env python3
"""Backfill £0 BREAKEVEN / UNKNOWN closes with broker-confirmed IG cash.

Modes:
  1) Legacy notes parse: notes contain level= but pnl was zero
  2) --from-ig: match learning-db stubs to IG transaction history by
     (market bucket, openLevel≈entry, side, size, day) and write ig_pnl_currency
     + journal cash rows. Safe while agent is running (no process kill).

Usage:
  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    python3 scripts/backfill_unknown_trade_pnl.py --dry-run
  PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    python3 scripts/backfill_unknown_trade_pnl.py --from-ig --days 3
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.learning_trade_policy import agent_trades_sql_clause
from system.pnl_math import classify_result, classify_result_gbp, realised_pnl_points

_LEVEL_RE = re.compile(r"level=([0-9]+(?:\.[0-9]+)?)")


def _market_bucket(epic: str = "", market: str = "") -> str:
    blob = f"{epic} {market}".lower()
    if "dow" in blob or "wall street" in blob or "wallstreet" in blob:
        return "DOW"
    if "nikkei" in blob or "japan" in blob:
        return "NIKKEI"
    if "gold" in blob:
        return "GOLD"
    if "eur" in blob and "usd" in blob:
        return "EURUSD"
    if "ftse" in blob or "uk 100" in blob:
        return "FTSE"
    return (epic or market or "").strip().upper()[:24]


def _backfill_from_notes(*, dry_run: bool) -> int:
    from data.learning_store import LearningStore
    from system.paths import data_dir

    store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
    clause = agent_trades_sql_clause()
    rows = store.conn.execute(
        f"""
        SELECT id, side, entry, exit, pnl_points, result, notes, size
        FROM trades
        WHERE closed_at IS NOT NULL
          AND {clause}
          AND result IN ('UNKNOWN', 'BREAKEVEN')
          AND (pnl_points IS NULL OR ABS(pnl_points) < 0.001)
          AND notes LIKE '%level=%'
        """
    ).fetchall()

    updated = 0
    for row in rows:
        notes = str(row["notes"] or "")
        m = _LEVEL_RE.search(notes)
        if not m:
            continue
        level = float(m.group(1))
        entry = float(row["entry"] or 0)
        if entry <= 0 or level <= 0:
            continue
        side = str(row["side"] or "BUY")
        pnl_pts = realised_pnl_points(side, entry, level)
        if abs(pnl_pts) < 0.001:
            continue
        result = classify_result(pnl_pts)
        size = float(row["size"] or 1)
        ig_pnl_est = round(pnl_pts * size, 2)
        if dry_run:
            print(
                f"would update id={row['id']} {result} pts={pnl_pts:+.2f} est_gbp={ig_pnl_est:+.2f}"
            )
        else:
            store.conn.execute(
                """
                UPDATE trades
                SET exit=?, pnl_points=?, result=?, ig_pnl_currency=COALESCE(ig_pnl_currency, ?)
                WHERE id=?
                """,
                (level, pnl_pts, result, ig_pnl_est, row["id"]),
            )
        updated += 1

    if not dry_run and updated:
        store.conn.commit()
        try:
            from system.setup_registry_refresh import refresh_setup_registry_from_store

            refresh_setup_registry_from_store(store, enabled=True)
        except Exception:
            pass

    print(f"{'would update' if dry_run else 'updated'} {updated} trade(s) via notes")
    return updated


def _fetch_ig_rows(*, days: int) -> list[dict[str, Any]]:
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import get_shared_rest_client
    from system.ig_transactions import ig_date_range_dd_mm_yyyy, parse_ig_transaction_row

    cred = try_load_credentials()
    if not cred.ok or not cred.credentials:
        raise RuntimeError("IG credentials unavailable")
    rest = get_shared_rest_client(cred.credentials)
    start, end = ig_date_range_dd_mm_yyyy(days_back=max(1, days))
    txns = rest.fetch_transactions(
        start, end, transaction_type="ALL_DEAL", page_size=500
    )
    out: list[dict[str, Any]] = []
    for txn in txns or []:
        row = parse_ig_transaction_row(txn)
        if row:
            out.append(row)
    return out


def _stub_rows(store: Any, *, since: str) -> list[Any]:
    return store.conn.execute(
        """
        SELECT id, closed_at, epic, market, side, entry, exit, size,
               pnl_points, ig_pnl_currency, result, ig_deal_id, deal_reference, notes
        FROM trades
        WHERE closed_at IS NOT NULL
          AND closed_at >= ?
          AND (
            ig_pnl_currency IS NULL
            OR (
              ABS(COALESCE(ig_pnl_currency, 0)) < 1e-9
              AND ABS(COALESCE(entry, 0) - COALESCE(exit, 0)) < 1e-9
            )
          )
        ORDER BY closed_at ASC, id ASC
        """,
        (since,),
    ).fetchall()


def _backfill_from_ig(*, dry_run: bool, days: int, since: str) -> dict[str, int]:
    from data.learning_store import LearningStore
    from system.paths import data_dir

    store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
    stubs = _stub_rows(store, since=since)
    ig_rows = _fetch_ig_rows(days=days)
    print(f"stubs={len(stubs)} ig_txns={len(ig_rows)} since={since}")

    claimed: set[str] = set()
    matched = 0
    updated = 0
    journaled = 0
    unmatched_samples: list[str] = []

    for stub in stubs:
        sid = int(stub["id"])
        try:
            entry = float(stub["entry"] or 0)
            size = float(stub["size"] or 0)
        except (TypeError, ValueError):
            continue
        side = str(stub["side"] or "").upper()
        bucket = _market_bucket(str(stub["epic"] or ""), str(stub["market"] or ""))
        day = str(stub["closed_at"] or "")[:10]
        best: dict[str, Any] | None = None
        best_score = -1.0
        for ig in ig_rows:
            ref = str(ig.get("ig_deal_id") or ig.get("deal_reference") or "").upper()
            if not ref or ref in claimed:
                continue
            ig_bucket = _market_bucket(
                str(ig.get("epic") or ""), str(ig.get("market") or "")
            )
            if bucket and ig_bucket and bucket != ig_bucket:
                continue
            ig_day = str(ig.get("closed_at") or "")[:10]
            if day and ig_day and day != ig_day:
                continue
            try:
                ig_entry = float(ig.get("entry") or 0)
                ig_size = float(ig.get("size") or 0)
            except (TypeError, ValueError):
                continue
            ig_side = str(ig.get("side") or "").upper()
            if entry <= 0 or ig_entry <= 0 or abs(entry - ig_entry) >= 0.051:
                continue
            if size > 0 and ig_size > 0 and abs(size - ig_size) > 1e-6:
                continue
            if side and ig_side and side != ig_side:
                continue
            score = 1000.0 - abs(entry - ig_entry) * 10.0
            if score > best_score:
                best = ig
                best_score = score
        if best is None:
            if len(unmatched_samples) < 5:
                unmatched_samples.append(
                    f"id={sid} {bucket} {side} entry={entry} day={day}"
                )
            continue
        matched += 1
        ref = str(best.get("ig_deal_id") or best.get("deal_reference") or "").upper()
        claimed.add(ref)
        pnl = float(best.get("ig_pnl_currency") or 0)
        result = str(best.get("result") or classify_result_gbp(pnl))
        exit_px = float(best.get("exit") or 0) or None
        pts = None
        if exit_px and entry:
            try:
                pts = realised_pnl_points(side or "BUY", entry, float(exit_px))
            except Exception:
                pts = None
        deal = str(stub["ig_deal_id"] or stub["deal_reference"] or sid)
        if dry_run:
            print(
                f"would id={sid} deal={deal[:18]} {result} gbp={pnl:+.2f} "
                f"exit={exit_px} ig_ref={ref}"
            )
            updated += 1
            continue
        ok = store.apply_ig_transaction_pnl(
            str(stub["deal_reference"] or ""),
            str(stub["ig_deal_id"] or ""),
            pnl,
            result,
            ig_close_deal_id=ref,
            exit_price=exit_px,
            entry_price=entry if entry > 0 else None,
            pnl_points=pts,
            emit_hooks=False,
        )
        if ok:
            updated += 1
            journaled += 1
        else:
            # Fallback direct SQL when deal-key lookup fails (id known).
            store.conn.execute(
                """
                UPDATE trades
                SET ig_pnl_currency=?, result=?, exit=COALESCE(?, exit),
                    pnl_points=COALESCE(?, pnl_points),
                    ig_close_deal_id=COALESCE(?, ig_close_deal_id)
                WHERE id=?
                """,
                (pnl, result, exit_px, pts, ref, sid),
            )
            store.conn.commit()
            updated += 1
            try:
                from diagnostics.performance_journal import upsert_journal_cash_close

                upsert_journal_cash_close(
                    deal_id=deal,
                    direction=side,
                    entry_price=entry if entry > 0 else None,
                    exit_price=exit_px,
                    realized_pnl_gbp=pnl,
                    closed_at=str(stub["closed_at"] or "") or None,
                )
                journaled += 1
            except Exception:
                pass

    if unmatched_samples:
        print("unmatched samples:")
        for s in unmatched_samples:
            print(" ", s)

    print(
        f"{'would update' if dry_run else 'updated'} matched={matched} "
        f"updated={updated} journaled={journaled}"
    )
    return {"matched": matched, "updated": updated, "journaled": journaled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from-ig",
        action="store_true",
        help="Match stubs to IG transaction history (broker SoT)",
    )
    parser.add_argument("--days", type=int, default=3, help="IG history lookback days")
    parser.add_argument(
        "--since",
        default="2026-07-03",
        help="Only touch learning closes on/after this date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    if args.from_ig:
        try:
            _backfill_from_ig(
                dry_run=bool(args.dry_run),
                days=int(args.days),
                since=str(args.since),
            )
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0

    _backfill_from_notes(dry_run=bool(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
