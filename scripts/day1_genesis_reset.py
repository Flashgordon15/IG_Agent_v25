#!/usr/bin/env python3
"""CLI entry — Day 1 Genesis Reset Protocol (run before Gate 1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.day1_genesis_reset import run_day1_genesis_reset


def main() -> int:
    manifest = run_day1_genesis_reset(force=True)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
