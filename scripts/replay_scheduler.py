#!/usr/bin/env python3
"""
Nightly replay pipeline — 06:15 Europe/London (in-agent scheduler) or manual.

  PYTHONPATH=src python3 scripts/replay_scheduler.py
  PYTHONPATH=src python3 scripts/replay_scheduler.py --scheduled
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.replay_scheduler_runner import run_replay_pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay fetch + signals + analysis")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Daily 06:15 job (allowed before 07:00 quiet window ends)",
    )
    args = parser.parse_args(argv)
    return run_replay_pipeline(scheduled=bool(args.scheduled))


if __name__ == "__main__":
    raise SystemExit(main())
