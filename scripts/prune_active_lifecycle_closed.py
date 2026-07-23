#!/usr/bin/env python3
"""Prune CLOSED rows from active_lifecycle_trades (dry-run by default).

Safe clutter cleanup — never deletes OPEN/ADOPTED/AGENT_MANAGED rows.
Archive optional JSONL before delete when --apply.

Usage:
  PYTHONPATH=src python3 scripts/prune_active_lifecycle_closed.py
  PYTHONPATH=src python3 scripts/prune_active_lifecycle_closed.py --older-than-hours 24 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("APP_MODE", "DEMO")
os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")


def _parse_ts(raw: str) -> datetime | None:
    s = str(raw or "").strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune CLOSED lifecycle rows")
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        help="Only prune CLOSED rows older than this many hours (default 24)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default is dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="Max rows to consider",
    )
    args = parser.parse_args()

    import sqlite3

    from runtime.active_lifecycle_trades import (
        STATE_BROKER_ANOMALY,
        STATE_CLOSED,
        ensure_lifecycle_table,
    )
    from system.paths import data_dir

    # Lifecycle registry lives on the learning DB (not triage_v31.db).
    db = data_dir() / "learning_db.sqlite3"
    if not db.is_file():
        # Follow symlink / bridge
        alt = Path("src/data/learning_db.sqlite3")
        if alt.is_file():
            db = alt.resolve()
    if not db.is_file():
        print(f"ERROR: learning DB not found at {db}")
        return 2
    conn = sqlite3.connect(str(db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    ensure_lifecycle_table(conn)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(args.older_than_hours))
    rows = conn.execute(
        """
        SELECT deal_id, epic, direction, size, lifecycle_state,
               last_broker_sync_at, last_event, notes
        FROM active_lifecycle_trades
        WHERE lifecycle_state IN (?, ?)
        ORDER BY last_broker_sync_at ASC
        LIMIT ?
        """,
        (STATE_CLOSED, STATE_BROKER_ANOMALY, int(args.limit)),
    ).fetchall()

    victims: list[dict] = []
    for r in rows:
        ts = _parse_ts(str(r["last_broker_sync_at"] or ""))
        if ts is None or ts > cutoff:
            continue
        victims.append({k: r[k] for k in r.keys()})

    print(
        f"CLOSED/ANOMALY candidates older than {args.older_than_hours}h: "
        f"{len(victims)} (scanned {len(rows)})"
    )
    for v in victims[:20]:
        print(
            f"  {v.get('deal_id')}  {v.get('epic')}  "
            f"{v.get('lifecycle_state')}  {v.get('last_broker_sync_at')}"
        )
    if len(victims) > 20:
        print(f"  ... +{len(victims) - 20} more")

    if not args.apply:
        print("DRY-RUN — pass --apply to archive+delete")
        return 0

    if not victims:
        print("Nothing to prune")
        return 0

    archive = data_dir() / "state" / "active_lifecycle_closed_archive.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("a", encoding="utf-8") as fh:
        for v in victims:
            fh.write(
                json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **v}, default=str)
                + "\n"
            )

    ids = [str(v["deal_id"]) for v in victims]
    conn.executemany(
        "DELETE FROM active_lifecycle_trades WHERE deal_id = ?",
        [(i,) for i in ids],
    )
    conn.commit()
    print(f"APPLIED — archived+deleted {len(ids)} rows → {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
