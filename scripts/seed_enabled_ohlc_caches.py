#!/usr/bin/env python3
"""Seed OHLC JSONL caches for all enabled instruments (Yahoo Finance)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from data.ohlc_yahoo_seeder import seed_enabled_instruments  # noqa: E402


def main() -> int:
    try:
        results = seed_enabled_instruments()
    except Exception as exc:
        print(
            f"seed_enabled_ohlc_caches failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if not results:
        print("No enabled instruments seeded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
