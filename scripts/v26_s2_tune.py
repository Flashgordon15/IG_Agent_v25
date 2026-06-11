#!/usr/bin/env python3
"""Tune S2 per-epic min_range_pct from OHLC history."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.s2_threshold_tuner import write_s2_threshold_snapshot


def main() -> int:
    path = write_s2_threshold_snapshot()
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    print("\n=== S2 threshold tune ===")
    for epic, row in sorted((data.get("by_epic") or {}).items()):
        print(
            f"  {row.get('market')}: min_range_pct={row.get('min_range_pct')} "
            f"wt={row.get('would_trade')}/{row.get('bars')} "
            f"({row.get('would_trade_rate'):.1%} target {row.get('target_wt_rate'):.1%})"
        )
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
