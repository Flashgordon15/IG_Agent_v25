#!/usr/bin/env python3
"""Phase A refresh — Finnhub calendar + full OHLC replay + S4 retrain + learning snapshot."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], label: str) -> int:
    print(f"\n--- {label} ---")
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**dict(__import__("os").environ), "PYTHONPATH": "src:v26"},
    ).returncode


def main() -> int:
    py = sys.executable
    steps = [
        (
            [py, str(ROOT / "scripts" / "ingest_finnhub_calendar.py"), "--days", "7"],
            "Finnhub",
        ),
        ([py, str(ROOT / "scripts" / "v26_ohlc_replay.py"), "--write"], "OHLC replay"),
        ([py, str(ROOT / "scripts" / "v26_s4_retrain.py")], "S4 retrain"),
        (
            [
                py,
                str(ROOT / "scripts" / "v26_l1_replay.py"),
                "--write",
                "--skip-features",
            ],
            "Learning snapshot",
        ),
    ]
    for cmd, label in steps:
        rc = _run(cmd, label)
        if rc != 0:
            print(f"Warning: {label} exited {rc}")
    print("\nPhase A refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
