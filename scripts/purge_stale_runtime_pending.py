#!/usr/bin/env python3
"""
Remove crossed-session ghost pending orders from runtime_state.json on disk.

Pending entries older than the load max-age (120s) block nothing at runtime
(load_pending_state skips them) but leave stale rows on disk until rewritten.

Usage:
  PYTHONPATH=src python3 scripts/purge_stale_runtime_pending.py
  PYTHONPATH=src python3 scripts/purge_stale_runtime_pending.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MAX_AGE_SEC = 120.0


def purge_stale_pending(*, path: Path | None = None, dry_run: bool = False) -> dict[str, int]:
    from system.paths import data_dir

    p = path or (data_dir() / "runtime_state.json")
    if not p.exists():
        return {"removed": 0, "kept": 0}

    data = json.loads(p.read_text(encoding="utf-8"))
    pending = data.get("pending") or {}
    orders = list(pending.get("orders") or [])
    now = time.time()
    kept: list[dict] = []
    removed = 0
    for item in orders:
        if not isinstance(item, dict):
            removed += 1
            continue
        try:
            ts = float(item.get("local_created_at") or 0.0)
        except (TypeError, ValueError):
            removed += 1
            continue
        age = now - ts if ts > 0 else float("inf")
        if age > MAX_AGE_SEC:
            removed += 1
            epic = item.get("epic", "?")
            print(f"  remove stale pending {epic} age={age:.0f}s ref={item.get('broker_deal_reference', '-')}")
        else:
            kept.append(item)

    if removed and not dry_run:
        data["pending"] = {"orders": kept}
        data["saved_at"] = now
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return {"removed": removed, "kept": len(kept)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge stale pending orders from runtime_state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    counts = purge_stale_pending(dry_run=bool(args.dry_run))
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"{mode}: removed={counts['removed']} kept={counts['kept']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
