#!/usr/bin/env python3
"""CLI supervision check for AI operators and overnight prep."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent supervision drift check")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Attempt to reload launchd supervision when plists are installed",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON only (for automation)",
    )
    args = parser.parse_args()

    from system.overnight_supervision import overnight_supervision_summary
    from system.supervision_monitor import (
        attempt_supervision_repair,
        evaluate_supervision_drift,
    )

    from system.shutdown_cleanup import agent_fully_stopped, repair_stale_watchdog_after_stop

    drift = evaluate_supervision_drift()
    summary = overnight_supervision_summary()
    repairs_attempted: list[str] = []
    if args.repair:
        if not drift.get("ok"):
            _ok, repair_detail = attempt_supervision_repair()
            repairs_attempted.append(repair_detail)
            drift = evaluate_supervision_drift()
            summary = overnight_supervision_summary()
        stopped_ok, _ = agent_fully_stopped()
        if stopped_ok:
            wd_ok, wd_detail = repair_stale_watchdog_after_stop()
            if wd_detail and "no repair needed" not in wd_detail:
                repairs_attempted.append(
                    f"watchdog cleanup: {'ok' if wd_ok else 'fail'} — {wd_detail}"
                )
                drift = evaluate_supervision_drift()
                summary = overnight_supervision_summary()
    repair_detail = "; ".join(repairs_attempted)
    if repairs_attempted:
        drift.setdefault("repairs_attempted", []).extend(repairs_attempted)

    payload = {
        "supervision_drift": drift,
        "overnight_supervision": summary,
        "repair": repair_detail,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        ok = bool(drift.get("ok"))
        print(f"Supervision drift: {'OK' if ok else 'ISSUES'}")
        for issue in drift.get("issues") or []:
            print(f"  ISSUE: {issue}")
        for warn in drift.get("warnings") or []:
            print(f"  WARN:  {warn}")
        print(f"Launchd watchdog: {summary.get('launchd_watchdog')}")
        print(f"Overnight armed: {summary.get('overnight_armed')}")
        if repair_detail:
            print(f"Repair: {repair_detail}")

    return 0 if drift.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
