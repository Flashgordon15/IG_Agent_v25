#!/usr/bin/env python3
"""Pre-launch gate — run before starting the agent from the desktop icon."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, label: str, env: dict[str, str]) -> int:
    print(f"\n--- {label} ---")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT), env=env)
    return int(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v25 pre-launch gate")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run live checks (agent must already be running)",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest contract suite (faster, not recommended)",
    )
    args = parser.parse_args()

    env = {**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")}
    py = sys.executable

    if args.live:
        # Agent must be running — do not run confirm_stopped (that expects zero processes).
        steps: list[tuple[str, list[str]]] = [
            (
                "Confirm started",
                [py, str(ROOT / "scripts" / "confirm_started.py")],
            ),
            (
                "Live pre-flight",
                [py, str(ROOT / "scripts" / "pre_flight_check.py"), "--live"],
            ),
        ]
    else:
        steps = [
            (
                "Confirm stopped",
                [py, str(ROOT / "scripts" / "confirm_stopped.py")],
            ),
            (
                "Static pre-flight",
                [py, str(ROOT / "scripts" / "pre_flight_check.py")],
            ),
        ]
        if not args.skip_tests:
            steps.append(
                (
                    "Pre-launch contract tests",
                    [
                        py,
                        "-m",
                        "pytest",
                        "tests/test_pre_launch_contracts.py",
                        "tests/test_api_server.py",
                        "tests/test_feeder_bar_snapshot.py",
                        "tests/test_agent_hardening.py",
                        "tests/test_deployment_verified.py",
                        "-q",
                        "--tb=line",
                    ],
                )
            )

    print("IG Agent v25 — PRE-LAUNCH GATE")
    print("=" * 44)

    for label, cmd in steps:
        rc = _run(cmd, label=label, env=env)
        if rc != 0:
            print(f"\nResult: FAIL at '{label}'")
            return rc

    print("\n" + "=" * 44)
    if args.live:
        print("PRE-LAUNCH GATE: PASS — agent is trading-ready")
    else:
        print("PRE-LAUNCH GATE: PASS — safe to launch from desktop icon")
        print("After launch (~3–5 min): run with --live to confirm stream + gates")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
