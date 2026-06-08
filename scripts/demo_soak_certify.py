#!/usr/bin/env python3
"""Demo soak certification — refresh L4/L5 forward cert + CERT ladder snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from expectancy.shadow_attribution import write_strategy_pnl_snapshot
    from research.certification import build_certification_payload
    from research.l4_forward import format_forward_status, write_forward_cert

    print("=== Demo soak certification ===\n")
    forward_path = write_forward_cert()
    print(f"Forward cert: {forward_path}")
    print(format_forward_status())

    shadow_path = write_strategy_pnl_snapshot(days=14)
    print(f"\nShadow attribution: {shadow_path}")

    cert = build_certification_payload()
    out = ROOT / "data_lake" / "state" / "v26_cert_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cert, indent=2), encoding="utf-8")
    print(f"\nCERT ladder: {out}")
    print(f"Passed {cert.get('passed_count')}/{cert.get('total_levels')} levels")
    for lv in cert.get("levels") or []:
        print(
            f"  {lv.get('id')} {lv.get('name')}: {lv.get('status')} — {lv.get('detail')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
