#!/usr/bin/env python3
"""S4 offline retrain — per-epic XGBoost from historic replay + ML store."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))

from research.s4_retrain import run_s4_retrain


def main() -> int:
    manifest = run_s4_retrain()
    print("\n=== S4 retrain ===")
    print(f"Version: {manifest.get('version')}")
    print(f"Labelled rows: {manifest.get('total_labelled_rows')}")
    print(f"Epics trained: {manifest.get('epics_trained')}")
    print(f"Veto eligible: {manifest.get('epics_veto_eligible')}")
    for epic, info in sorted((manifest.get("by_epic") or {}).items()):
        if info.get("ok"):
            print(
                f"  {epic}: val_wr={info.get('val_wr')} "
                f"eligible={info.get('veto_eligible')} "
                f"min_p={info.get('recommended_min_prob')}"
            )
        else:
            print(f"  {epic}: SKIP ({info.get('error')}) rows={info.get('rows')}")
    from pathlib import Path as _Path

    path = _Path(ROOT) / "data_lake" / "models" / "s4" / "manifest.json"
    print(f"\nManifest: {path}")
    print("Enable ml_veto per epic in config_v26.json after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
