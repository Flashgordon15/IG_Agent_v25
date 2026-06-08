#!/usr/bin/env python3
"""Print or write L0–L5 certification ladder (CERT tab source)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v26"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write data_lake/state/v26_cert_snapshot.json",
    )
    args = parser.parse_args()

    from research.certification import build_certification_payload

    cert = build_certification_payload()
    if args.write:
        out = ROOT / "data_lake" / "state" / "v26_cert_snapshot.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cert, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
    for lv in cert.get("levels") or []:
        print(
            f"{lv.get('id')} [{lv.get('status')}] {lv.get('pct')}% — "
            f"{lv.get('name')}: {lv.get('detail')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
