#!/usr/bin/env python3
"""Verify IG Agent v25 is fully stopped after dashboard Stop Agent."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from system.shutdown_cleanup import agent_fully_stopped, stopped_verification_checks

    ok, issues = agent_fully_stopped()
    checks = stopped_verification_checks(issues)
    print()
    print("IG Agent v25 — CONFIRM STOPPED")
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
