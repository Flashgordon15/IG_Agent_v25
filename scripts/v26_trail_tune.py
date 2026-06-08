#!/usr/bin/env python3
"""Tune per-epic trail trigger/distance from replay MFE/MAE."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.trail_tuner import write_trail_tune_snapshot


def main() -> int:
    path = write_trail_tune_snapshot()
    data = json.loads(path.read_text(encoding="utf-8"))
    print("\n=== Trail MFE/MAE tune ===")
    for epic, row in sorted((data.get("by_epic") or {}).items()):
        print(
            f"  {row.get('market')}: trigger={row.get('trail_trigger_atr_multiple')}×ATR "
            f"distance={row.get('trail_distance_atr_multiple')}×ATR "
            f"capture={row.get('median_capture_ratio'):.2f} "
            f"(n={row.get('fired_signals')}) "
            f"MFE_R={row.get('mfe_r_median')} MAE_R={row.get('mae_r_median')}"
        )
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
