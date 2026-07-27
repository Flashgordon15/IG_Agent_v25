#!/usr/bin/env python3
"""CLI: trade lifecycle witness + loss autopsy (read-only).

Examples::

  PYTHONPATH=src .venv/bin/python3 scripts/trade_lifecycle_witness.py \\
    --deal-id DIAAAAXBASBTJFTUJLTYTK

  PYTHONPATH=src .venv/bin/python3 scripts/trade_lifecycle_witness.py \\
    --loss-autopsy --day 2026-07-24 --write

  PYTHONPATH=src .venv/bin/python3 scripts/trade_lifecycle_witness.py \\
    --last-n 5

Never places orders. Never removes bleed locks / pause markers.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from diagnostics.trade_lifecycle_witness import (  # noqa: E402
    default_data_root,
    reconstruct_lifecycle,
    write_loss_autopsy,
    load_journal_rows,
    dedupe_journal_by_deal,
)


def _today_london() -> str:
    return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="Trade lifecycle witness / loss autopsy")
    ap.add_argument("--deal-id", help="Reconstruct one deal_id")
    ap.add_argument("--last-n", type=int, help="Reconstruct last N journal closes")
    ap.add_argument("--day", default=None, help="Calendar day YYYY-MM-DD (default: London today)")
    ap.add_argument("--loss-autopsy", action="store_true", help="Build loss autopsy report")
    ap.add_argument("--since-reopen", action="store_true", help="Limit autopsy to post-reopen closes")
    ap.add_argument("--top-n", type=int, default=8, help="Top losers to deep-dive")
    ap.add_argument("--write", action="store_true", help="Write reports/loss_autopsy_YYYY-MM-DD.md(+json)")
    ap.add_argument("--data-root", type=Path, default=None)
    args = ap.parse_args()

    root = args.data_root or default_data_root()
    day = args.day or _today_london()

    if args.loss_autopsy:
        md_path, json_path, report = write_loss_autopsy(
            day=day,
            data_root=root,
            top_n=args.top_n,
            since_reopen=args.since_reopen,
            write_json=True,
        )
        if not args.write:
            # write_loss_autopsy always writes; --write is documented as explicit.
            # Keep files (operator asked for the report path).
            pass
        fund = report.get("fundamentals_followed") or {}
        summary = report.get("summary") or {}
        print(f"Wrote {md_path}")
        if json_path:
            print(f"Wrote {json_path}")
        print(f"Verdict: {fund.get('verdict')}")
        print(
            f"closes={summary.get('closes')} losers={summary.get('losers')} "
            f"net={summary.get('net_gbp')} "
            f"APP={fund.get('app')} LOGIC={fund.get('logic')} UNKNOWN={fund.get('unknown')}"
        )
        from diagnostics.trade_lifecycle_witness import _bullet_findings

        print("Findings:")
        for f in _bullet_findings(report):
            print(f"  - {f}")
        return 0

    if args.deal_id:
        lc = reconstruct_lifecycle(args.deal_id, data_root=root)
        print(json.dumps(lc.to_dict(), indent=2, default=str))
        return 0

    if args.last_n:
        rows = load_journal_rows(root, day=day if args.day else None)
        if not args.day:
            rows = load_journal_rows(root)
        by = dedupe_journal_by_deal(rows)
        # preserve journal order by timestamp
        ordered = sorted(
            by.values(),
            key=lambda r: str(r.get("Timestamp") or ""),
        )[-int(args.last_n) :]
        out = []
        for r in ordered:
            lc = reconstruct_lifecycle(
                str(r.get("DealID")),
                data_root=root,
                journal_row=r,
            )
            out.append(lc.to_dict())
        print(json.dumps(out, indent=2, default=str))
        return 0

    ap.error("Specify --loss-autopsy, --deal-id, or --last-n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
