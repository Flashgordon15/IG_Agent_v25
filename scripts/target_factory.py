#!/usr/bin/env python3
"""
Target factory gatekeeper — live-fire reconciliation authority.

Exit 0 only when trading_ledger.json reports:
  - net_pnl_gbp >= £1,000
  - win_rate >= 60%
  - zero phantom rows
  - zero INTEGRITY_ABORT blockers
  - architecture pytest gates pass
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from target_reconciliation.live_fire_ledger import (  # noqa: E402
    LEDGER_PATH,
    TARGET_NET_PNL_GBP,
    TARGET_WIN_RATE,
    reconcile_trading_ledger,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-fire target factory gatekeeper")
    parser.add_argument("--hours", type=float, default=720.0, help="Ledger lookback hours")
    parser.add_argument("--json", action="store_true", help="Print full ledger JSON")
    args = parser.parse_args()

    ledger = reconcile_trading_ledger(hours=float(args.hours))
    if args.json:
        print(json.dumps(ledger, indent=2))

    metrics = ledger.get("metrics") or {}
    blockers = ledger.get("blockers") or []
    targets_met = bool(ledger.get("targets_met"))

    print(
        f"TARGET-FACTORY metrics: net_pnl=£{metrics.get('net_pnl_gbp')} "
        f"win_rate={float(metrics.get('win_rate') or 0) * 100:.1f}% "
        f"closed={metrics.get('closed_trades')} "
        f"targets_met={targets_met}"
    )
    print(f"TARGET-FACTORY ledger: {LEDGER_PATH}")

    if blockers:
        print("TARGET-FACTORY blockers:")
        for blocker in blockers[:8]:
            print(f"  - {json.dumps(blocker, default=str)[:400]}")

    if not targets_met:
        print(
            f"TARGET-FACTORY FAIL — require net_pnl>=£{TARGET_NET_PNL_GBP:.0f} "
            f"and win_rate>={TARGET_WIN_RATE * 100:.0f}% with clean broker ledger"
        )
        return 1

    print("TARGET-FACTORY PASS — live-fire reconciliation locked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
