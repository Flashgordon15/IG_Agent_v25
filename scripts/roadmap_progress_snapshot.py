#!/usr/bin/env python3
"""Write daily roadmap progress snapshot for dashboard history."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from api.roadmap_progress import build_roadmap_progress

    payload = build_roadmap_progress(history_days=14, write_snapshot=True)
    out = ROOT / "data_lake" / "state" / "roadmap_progress_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Roadmap overall: {payload.get('overall_pct')}% · milestone {payload.get('milestone')}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
