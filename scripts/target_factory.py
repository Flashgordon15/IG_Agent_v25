#!/usr/bin/env python3
"""
Target factory gatekeeper — live-fire reconciliation authority.

Exit 0 only when trading_ledger.json reports:
  - net_pnl_gbp >= £1,000
  - win_rate >= 60%
  - zero phantom rows
  - zero INTEGRITY_ABORT blockers
  - architecture pytest gates pass

Milestone baseline: only trades with timestamp > 2026-06-23T09:00:00Z count
toward the £1,000 / 60% recovery target.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from target_reconciliation.live_fire_ledger import (  # noqa: E402
    LEDGER_PATH,
    MILESTONE_BASELINE_UTC,
    TARGET_NET_PNL_GBP,
    TARGET_WIN_RATE,
    _filter_rows_since,
    _ledger_metrics,
    reconcile_trading_ledger,
    write_trading_ledger,
)

# Hardcoded production milestone — recovery session accounting starts here.
MILESTONE_CUTOFF_Z = "2026-06-23T09:00:00Z"


def _milestone_cutoff_dt() -> datetime:
    text = MILESTONE_CUTOFF_Z.replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _parse_milestone_since(raw: str | None) -> datetime | None:
    if raw is None or str(raw).strip().lower() in ("", "none", "all"):
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _milestone_metrics_from_ledger_file() -> dict[str, Any]:
    """
    Read trading_ledger.json trades/closed_trades and discard pre-milestone rows.
  """
    if not LEDGER_PATH.is_file():
        return {
            "net_pnl_gbp": 0.0,
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "closed_trades": 0,
        }
    payload = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    trades = payload.get("closed_trades") or payload.get("trades") or []
    if not isinstance(trades, list):
        trades = []
    milestone_rows = _filter_rows_since(
        [t for t in trades if isinstance(t, dict)],
        since=_milestone_cutoff_dt(),
    )
    return _ledger_metrics(milestone_rows)


def _apply_milestone_baseline_to_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Recompute targets from milestone-filtered trades array on disk."""
    milestone_metrics = _milestone_metrics_from_ledger_file()
    out = dict(ledger)
    out["metrics"] = milestone_metrics
    out["milestone"] = {
        "since_utc": MILESTONE_CUTOFF_Z,
        "baseline_net_pnl_gbp": 0.0,
        "tracking": "post_milestone_only",
    }
    closed = out.get("closed_trades") or out.get("trades") or []
    if isinstance(closed, list):
        out["closed_trades"] = _filter_rows_since(
            [r for r in closed if isinstance(r, dict)],
            since=_milestone_cutoff_dt(),
        )
    arch_ok = bool((out.get("architecture") or {}).get("ok"))
    blockers = out.get("blockers") or []
    phantoms = [b for b in blockers if b.get("kind") == "phantom_rows"]
    integrity = [b for b in blockers if b.get("kind") == "integrity_abort"]
    out["targets_met"] = (
        arch_ok
        and not phantoms
        and not integrity
        and milestone_metrics["net_pnl_gbp"] >= TARGET_NET_PNL_GBP
        and milestone_metrics["win_rate"] >= TARGET_WIN_RATE
        and milestone_metrics["closed_trades"] > 0
    )
    write_trading_ledger(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-fire target factory gatekeeper")
    parser.add_argument("--hours", type=float, default=720.0, help="Ledger lookback hours")
    parser.add_argument(
        "--milestone-since",
        default=MILESTONE_CUTOFF_Z,
        help="Only count closed trades after this UTC instant",
    )
    parser.add_argument("--json", action="store_true", help="Print full ledger JSON")
    args = parser.parse_args()

    milestone = _parse_milestone_since(args.milestone_since) or _milestone_cutoff_dt()
    ledger = reconcile_trading_ledger(hours=float(args.hours), milestone_since=milestone)
    ledger = _apply_milestone_baseline_to_ledger(ledger)

    if args.json:
        print(json.dumps(ledger, indent=2))

    metrics = ledger.get("metrics") or {}
    metrics_all = ledger.get("metrics_all_time") or {}
    blockers = ledger.get("blockers") or []
    targets_met = bool(ledger.get("targets_met"))
    milestone_info = ledger.get("milestone") or {}

    print(
        f"TARGET-FACTORY milestone: since={milestone_info.get('since_utc', MILESTONE_CUTOFF_Z)} "
        f"baseline=£0.00"
    )
    print(
        f"TARGET-FACTORY metrics: net_pnl=£{metrics.get('net_pnl_gbp')} "
        f"win_rate={float(metrics.get('win_rate') or 0) * 100:.1f}% "
        f"closed={metrics.get('closed_trades')} "
        f"targets_met={targets_met}"
    )
    print(
        f"TARGET-FACTORY all_time: net_pnl=£{metrics_all.get('net_pnl_gbp')} "
        f"win_rate={float(metrics_all.get('win_rate') or 0) * 100:.1f}% "
        f"closed={metrics_all.get('closed_trades')}"
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
