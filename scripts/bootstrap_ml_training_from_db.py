#!/usr/bin/env python3
"""Bootstrap ml_training_store.jsonl from labeled agent closes in learning_db."""

from __future__ import annotations

import argparse
import json

from data.ml_training_store import MLTrainingStore, default_store_path
from execution.ml_training_hooks import hydrate_ml_entry_from_deal, record_ml_exit_for_deal
from execution.ml_training_hooks import configure_ml_training
from system.learning_trade_policy import agent_trades_sql_clause
from system.paths import data_dir


def _existing_deals(path) -> set[str]:
    deals: set[str] = set()
    if not path.is_file():
        return deals
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            did = str(obj.get("deal_id") or "").strip()
            if did:
                deals.add(did)
        except json.JSONDecodeError:
            continue
    return deals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from data.learning_store import LearningStore

    store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
    ml_path = default_store_path()
    seen = _existing_deals(ml_path)
    clause = agent_trades_sql_clause()
    rows = store.conn.execute(
        f"""
        SELECT ig_deal_id, deal_reference, exit, pnl_points, result, size,
               ig_pnl_currency, notes
        FROM trades
        WHERE closed_at IS NOT NULL
          AND {clause}
          AND result IN ('WIN', 'LOSS', 'BREAKEVEN')
          AND ig_deal_id IS NOT NULL
        ORDER BY closed_at ASC
        """
    ).fetchall()

    ml_store = MLTrainingStore()
    configure_ml_training(ml_store=ml_store)
    added = 0
    for row in rows:
        deal_id = str(row["ig_deal_id"] or row["deal_reference"] or "").strip()
        if not deal_id or deal_id in seen:
            continue
        entry = hydrate_ml_entry_from_deal(deal_id)
        if entry is None:
            continue
        size = float(row["size"] or 1.0) or 1.0
        pts = float(row["pnl_points"] or 0.0)
        gbp = (
            float(row["ig_pnl_currency"])
            if row["ig_pnl_currency"] is not None
            else pts * size
        )
        if args.dry_run:
            print(f"would add {deal_id} {row['result']} gbp={gbp:+.2f}")
            added += 1
            continue
        ml_store.record_entry(deal_id, entry)
        record_ml_exit_for_deal(
            deal_id,
            ig_pnl=gbp,
            result=str(row["result"] or ""),
            exit_price=float(row["exit"] or 0.0),
            pts_pnl=pts,
            exit_reason="bootstrap_from_db",
        )
        seen.add(deal_id)
        added += 1

    print(f"{'would add' if args.dry_run else 'added'} {added} ML record(s)")
    if not args.dry_run:
        print("total ML records", MLTrainingStore().record_count())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
