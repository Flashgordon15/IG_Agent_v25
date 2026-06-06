#!/usr/bin/env python3
"""Verify IG Agent v25 is fully stopped after dashboard Stop Agent."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from system.shutdown_cleanup import agent_fully_stopped

    ok, issues = agent_fully_stopped()
    print()
    print("IG Agent v25 — CONFIRM STOPPED")
    print("=" * 40)
    checks = [
        ("No main.py process", "main.py still running" not in issues),
        ("No watchdog process", "watchdog.sh still running" not in issues),
        ("Port 8080 free", "port 8080 still bound" not in issues),
        ("No instance lock", "instance lock file present" not in issues),
        ("No watchdog.pid", "watchdog.pid present" not in issues),
    ]
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
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
