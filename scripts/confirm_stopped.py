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
        ensure_supervision_utilities_executable,
        repair_stale_watchdog_after_stop,
        stopped_verification_checks,
        _list_main_py_pids,
        _port_bound,
    )

    if args.repair:
        util_ok, util_repaired = ensure_supervision_utilities_executable()
        if util_repaired:
            print(
                "Repair: chmod +x "
                + ", ".join(util_repaired[:6])
                + (" …" if len(util_repaired) > 6 else "")
            )
        elif not util_ok:
            print("Repair: WARN — some supervision utilities still not executable")
        repaired, detail = repair_stale_watchdog_after_stop()
        print(f"Repair: {'OK' if repaired else 'FAIL'} — {detail}")

    # Fast path: closed :8080 with no agent process is the expected post-Stop state.
    if not _port_bound() and not _list_main_py_pids():
        ok, issues = agent_fully_stopped()
        if ok:
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
            print("→ FULLY STOPPED — safe to close browser tab")
            return 0

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
