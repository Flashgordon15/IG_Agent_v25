#!/usr/bin/env python3
"""Reset points engine to HEALTHY for demo E2E after a drawdown session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.paths import data_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset points state to HEALTHY")
    parser.add_argument(
        "--cumulative",
        type=float,
        default=6.0,
        help="Cumulative score (>6 = HEALTHY)",
    )
    args = parser.parse_args()

    path = data_dir() / "state" / "points_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "cumulative": args.cumulative,
        "cumulative_points": args.cumulative,
        "state": "HEALTHY",
        "session_score": 0.0,
        "last_trade_score": 0.0,
        "consecutive_losses": 0,
        "signals_to_skip": 0,
        "recovery_wins": 0,
        "bootstrap_wins": 0,
        "day_stopped": False,
        "stop_latched": False,
        "last_nominal": "HEALTHY",
        "rapid_cooldown_until": 0.0,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {path} — state HEALTHY cumulative={args.cumulative}")
    print("Restart the agent (or wait for next tick) to load new points state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
