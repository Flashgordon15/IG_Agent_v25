#!/usr/bin/env python3
"""Nightly v26 learning refresh — run after session flatten (~22:30 UTC)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    py = sys.executable
    cmds = [
        (
            [py, str(ROOT / "scripts" / "roadmap_progress_snapshot.py")],
            "roadmap progress snapshot",
        ),
        (
            [py, str(ROOT / "scripts" / "ingest_finnhub_calendar.py"), "--days", "7"],
            "Finnhub calendar",
        ),
        (
            [py, str(ROOT / "scripts" / "v26_learning_pack.py"), "--skip-ohlc"],
            "learning pack",
        ),
        ([py, str(ROOT / "scripts" / "v26_s4_retrain.py")], "S4 retrain"),
        (
            [py, str(ROOT / "scripts" / "demo_soak_certify.py")],
            "demo forward cert",
        ),
        (
            [py, str(ROOT / "scripts" / "v26_ml_veto_promote.py")],
            "ml_veto promote",
        ),
        (
            [py, str(ROOT / "scripts" / "build_feature_store.py"), "--days", "7"],
            "feature store (7d threshold replay)",
        ),
    ]
    env = {**dict(__import__("os").environ), "PYTHONPATH": "src:v26"}
    for cmd, label in cmds:
        print(f"\n--- nightly: {label} ---")
        rc = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
        if rc != 0:
            print(f"Warning: {label} exited {rc}")
    print("\nNightly v26 refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
