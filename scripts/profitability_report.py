#!/usr/bin/env python3
"""
Per-epic P&L report and optional learning DB reconcile.

  PYTHONPATH=src python3 scripts/profitability_report.py
  PYTHONPATH=src python3 scripts/profitability_report.py --reconcile
  PYTHONPATH=src python3 scripts/profitability_report.py --days 14
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LEGACY_PNL_POINTS_CAP = 300.0  # pre-£/pt-fix rows often show 200–650 pts


def _epic_map() -> dict[str, str]:
    from system.config_loader import get_config

    cfg = get_config()
    by_epic: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for _key, inst in (cfg.as_dict().get("instruments") or {}).items():
        epic = str(inst.get("epic") or "").strip()
        name = str(inst.get("name") or _key).strip().lower()
        if epic:
            by_epic[epic] = epic
            by_name[name] = epic
            by_name[_key.replace("_", " ")] = epic
    return {**by_name, **by_epic}


def reconcile_db(db_path: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Backfill missing epic; tag legacy suspicious pnl_points in notes."""
    epic_map = _epic_map()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    stats = {"epic_filled": 0, "legacy_tagged": 0}

    cur.execute(
        "SELECT id, market, epic, pnl_points, notes, ig_pnl_currency FROM trades WHERE dry_run=0"
    )
    for row in cur.fetchall():
        tid = int(row["id"])
        epic = str(row["epic"] or "").strip()
        market = str(row["market"] or "").strip().lower()
        notes = str(row["notes"] or "")
        pnl_pts = float(row["pnl_points"] or 0)
        ig_pnl = row["ig_pnl_currency"]

        updates: list[str] = []
        params: list[object] = []

        if not epic and market:
            resolved = epic_map.get(market) or epic_map.get(market.replace(" ", "_"))
            if resolved:
                updates.append("epic=?")
                params.append(resolved)
                stats["epic_filled"] += 1

        if (
            abs(pnl_pts) > LEGACY_PNL_POINTS_CAP
            and ig_pnl is None
            and "legacy_pnl_suspect" not in notes
        ):
            updates.append("notes=COALESCE(notes,'') || ?")
            params.append(" | legacy_pnl_suspect: pre-£/pt-fix row")
            stats["legacy_tagged"] += 1

        if updates and not dry_run:
            params.append(tid)
            cur.execute(
                f"UPDATE trades SET {', '.join(updates)} WHERE id=?",
                params,
            )

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def print_report(db_path: Path, *, days: int = 7) -> int:
    epic_map = _epic_map()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    cur.execute(
        """
        SELECT epic, market, result, pnl_points, ig_pnl_currency, adjusted_confidence,
               datetime(closed_at) closed_at, notes
        FROM trades
        WHERE dry_run=0 AND result IN ('WIN','LOSS','BREAKEVEN')
          AND closed_at IS NOT NULL AND date(closed_at) >= ?
        ORDER BY closed_at DESC
        """,
        (since,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    clean = [r for r in rows if "legacy_pnl_suspect" not in str(r.get("notes") or "")]
    legacy_n = len(rows) - len(clean)

    by_epic: dict[str, dict] = defaultdict(
        lambda: {"W": 0, "L": 0, "BE": 0, "pts": 0.0, "gbp": 0.0, "conf": []}
    )
    for r in clean:
        epic = str(r.get("epic") or r.get("market") or "unknown")
        res = str(r.get("result") or "")
        bucket = by_epic[epic]
        if res == "WIN":
            bucket["W"] += 1
        elif res == "LOSS":
            bucket["L"] += 1
        else:
            bucket["BE"] += 1
        bucket["pts"] += float(r.get("pnl_points") or 0)
        if r.get("ig_pnl_currency") is not None:
            bucket["gbp"] += float(r["ig_pnl_currency"])
        if r.get("adjusted_confidence"):
            bucket["conf"].append(float(r["adjusted_confidence"]))

    print()
    print("IG Agent v25 — Profitability Report")
    print("=" * 56)
    print(f"Window: last {days} days (since {since})")
    print(f"Closed trades: {len(clean)} clean, {legacy_n} legacy-tagged excluded")
    print()

    if not clean:
        print("No confirmed closes in window.")
        return 0

    total_w = sum(b["W"] for b in by_epic.values())
    total_l = sum(b["L"] for b in by_epic.values())
    wr = 100.0 * total_w / (total_w + total_l) if (total_w + total_l) else 0.0
    print(f"Portfolio: {total_w}W / {total_l}L  ({wr:.1f}% WR)")
    print()
    print(f"{'Epic':<28} {'W/L':>7} {'WR%':>6} {'Pts':>10} {'GBP':>10} {'AvgConf':>8}")
    print("-" * 56)

    for epic, b in sorted(by_epic.items(), key=lambda x: -(x[1]["W"] + x[1]["L"])):
        t = b["W"] + b["L"]
        ewr = 100.0 * b["W"] / t if t else 0.0
        avg_c = sum(b["conf"]) / len(b["conf"]) if b["conf"] else 0.0
        gbp_s = f"{b['gbp']:+.2f}" if b["gbp"] else "—"
        print(
            f"{epic:<28} {b['W']:>3}/{b['L']:<3} {ewr:>5.1f}% "
            f"{b['pts']:>+10.1f} {gbp_s:>10} {avg_c:>7.0f}%"
        )

    # Shadow blockers (today)
    shadow = ROOT / "src/data/shadow_log.jsonl"
    if shadow.is_file():
        today = datetime.now().date().isoformat()
        blocks: dict[str, int] = defaultdict(int)
        fired = 0
        for line in shadow.open(encoding="utf-8"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(row.get("timestamp", "")).startswith(today):
                continue
            if row.get("would_have_fired"):
                fired += 1
            else:
                gb = str(row.get("gate_blocked_at") or "other")
                blocks[gb] += 1
        if fired or blocks:
            print()
            print(f"Shadow today: {fired} would-fire, {sum(blocks.values())} blocked")
            for k, v in sorted(blocks.items(), key=lambda x: -x[1])[:5]:
                print(f"  {k}: {v}")

    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Profitability report / DB reconcile")
    parser.add_argument(
        "--reconcile", action="store_true", help="Backfill epic + tag legacy rows"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Reconcile without writing"
    )
    parser.add_argument("--days", type=int, default=7, help="Report window (days)")
    args = parser.parse_args()

    from system.config_loader import get_config

    db_path = Path(get_config().learning_db)
    if not db_path.is_file():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    if args.reconcile:
        stats = reconcile_db(db_path, dry_run=args.dry_run)
        mode = "dry-run" if args.dry_run else "applied"
        print(f"Reconcile ({mode}): {stats}")
    return print_report(db_path, days=args.days)


if __name__ == "__main__":
    raise SystemExit(main())
