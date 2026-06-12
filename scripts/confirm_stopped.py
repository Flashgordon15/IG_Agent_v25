#!/usr/bin/env python3
"""Verify IG Agent v29 is fully stopped after dashboard Stop Agent."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify IG Agent is fully stopped")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Kill orphaned watchdog when launchd is not loaded",
    )
    args = parser.parse_args()

    from system.shutdown_cleanup import (
        agent_fully_stopped,
        repair_stale_watchdog_after_stop,
        stopped_verification_checks,
    )

    if args.repair:
        repaired, detail = repair_stale_watchdog_after_stop()
        print(f"Repair: {'OK' if repaired else 'FAIL'} — {detail}")

    ok, issues = agent_fully_stopped()
    checks = stopped_verification_checks(issues)
    print()
    from system.app_identity import APP_DISPLAY_NAME, APP_VERSION_LABEL

    print(f"{APP_DISPLAY_NAME} {APP_VERSION_LABEL} — CONFIRM STOPPED")
    print("=" * 40)
    for row in checks:
        label = str(row.get("label") or "")
        passed = bool(row.get("ok"))
        detail = str(row.get("detail") or "").strip()
        suffix = f" — {detail}" if detail else ""
        print(f"[{'PASS' if passed else 'FAIL'}] {label}{suffix}")
    print("=" * 40)
    if ok:
        print("→ FULLY STOPPED — safe to close browser tab")
        return 0
    print("→ NOT FULLY STOPPED — remaining issues:")
    for issue in issues:
        print(f"  - {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
