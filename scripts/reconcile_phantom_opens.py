#!/usr/bin/env python3
"""
Close learning-db rows still marked open but absent on the IG broker book.

Broker-authoritative: requires REST credentials. Use before today's session so
WR/ML metrics are not inflated by ghost opens.

Usage:
  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    PYTHONPATH=src python3 scripts/reconcile_phantom_opens.py --dry-run
  IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\
    PYTHONPATH=src python3 scripts/reconcile_phantom_opens.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _broker_open_deal_ids(rest: Any) -> set[str]:
    deals: set[str] = set()
    if hasattr(rest, "open_positions"):
        rows = rest.open_positions() or []
    elif hasattr(rest, "fetch_open_positions"):
        rows = rest.fetch_open_positions() or []
    else:
        return deals
    for row in rows:
        if not isinstance(row, dict):
            continue
        pos = row.get("position") or row
        deal = pos.get("dealId") or pos.get("deal_id") or row.get("dealId")
        if deal:
            deals.add(str(deal).strip())
    return deals


def reconcile_phantom_opens(*, dry_run: bool = True) -> dict[str, int]:
    from data.learning_store import LearningStore
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import get_shared_rest_client
    from system.paths import data_dir

    cred = try_load_credentials()
    if not cred.ok or not cred.credentials:
        raise RuntimeError("IG credentials unavailable — cannot reconcile phantoms")

    rest = get_shared_rest_client(cred.credentials)
    broker_deals = _broker_open_deal_ids(rest)
    store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
    active = store.active_trades()

    kept = 0
    closed = 0
    for row in active:
        keys = row.keys()
        deal_id = str(row["ig_deal_id"] or "") if "ig_deal_id" in keys else ""
        trade_id = int(row["id"])
        epic = str(row["epic"] or "")
        entry = float(row["entry"] or 0)
        if deal_id and deal_id in broker_deals:
            kept += 1
            continue
        if dry_run:
            closed += 1
            print(f"  would close id={trade_id} epic={epic} deal={deal_id or '-'}")
            continue
        # Leave ig_pnl_currency NULL (CANCELLED + flat exit) so IG transaction
        # sync can land broker cash later — never write BREAKEVEN £0 stubs.
        store.close_trade(
            trade_id,
            exit_price=entry,
            pnl_points=0.0,
            result="CANCELLED",
            notes="session_phantom_reconcile",
        )
        closed += 1

    return {
        "broker_open": len(broker_deals),
        "db_open_before": len(active),
        "kept": kept,
        "closed": closed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile phantom open trades in learning DB")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not close rows")
    args = parser.parse_args()

    try:
        counts = reconcile_phantom_opens(dry_run=bool(args.dry_run))
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"{mode}: broker_open={counts['broker_open']} db_open={counts['db_open_before']} "
          f"kept={counts['kept']} closed={counts['closed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
