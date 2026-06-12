#!/usr/bin/env python3
"""Operator E2E lifecycle check — stopped state, supervision, deployment tests."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, label: str) -> tuple[bool, str]:
    print(f"\n--- {label} ---")
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.strip())
    ok = proc.returncode == 0
    print(f"→ {'PASS' if ok else 'FAIL'} ({label})")
    return ok, out.strip().splitlines()[-1] if out.strip() else f"exit {proc.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent lifecycle E2E check")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Repair supervision/watchdog when agent is stopped",
    )
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="Skip deployment verification pytest",
    )
    args = parser.parse_args()

    py = sys.executable
    results: list[tuple[str, bool]] = []

    ok, _ = _run(
        [py, str(ROOT / "scripts" / "confirm_stopped.py")]
        + (["--repair"] if args.repair else []),
        label="confirm_stopped",
    )
    results.append(("confirm_stopped", ok))

    ok, _ = _run(
        [py, str(ROOT / "scripts" / "supervision_check.py")]
        + (["--repair"] if args.repair else []),
        label="supervision_check",
    )
    results.append(("supervision_check", ok))

    if not args.skip_deploy:
        ok, _ = _run(
            [
                py,
                "-m",
                "pytest",
                "tests/test_shutdown_lifecycle.py",
                "tests/test_deployment_verified.py",
                "-q",
                "--tb=line",
            ],
            label="deployment + lifecycle pytest",
        )
        results.append(("pytest", ok))

    print("\n" + "=" * 48)
    print("LIFECYCLE E2E SUMMARY")
    print("=" * 48)
    all_ok = True
    for name, passed in results:
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {name}")
        all_ok = all_ok and passed

    if all_ok:
        print("\n→ ALL CHECKS PASSED")
        return 0
    print("\n→ ISSUES REMAIN — review output above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
