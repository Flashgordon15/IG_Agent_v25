#!/usr/bin/env python3
"""Quick scalping framework smoke — unit E2E + optional live DEMO routing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    print("=== Scalping smoke E2E ===\n")

    env = {**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")}

    steps = [
        (
            "Unit + mock E2E",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_scalping_framework.py",
                "tests/test_scalping_e2e_smoke.py",
                "-v",
                "--tb=short",
                "-k",
                "not test_live_demo_routing",
            ],
        ),
        (
            "Live DEMO routing (skipped if no credentials)",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_scalping_e2e_smoke.py::ScalpingLiveRoutingSmoke",
                "-v",
                "--tb=short",
            ],
        ),
    ]

    rc = 0
    for label, cmd in steps:
        print(f"--- {label} ---")
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
        if proc.returncode != 0:
            rc = proc.returncode
            print(f"FAILED ({label})\n")
        else:
            print(f"OK ({label})\n")

    if rc == 0:
        print("SCALPING SMOKE E2E: PASS")
    else:
        print("SCALPING SMOKE E2E: FAIL")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
