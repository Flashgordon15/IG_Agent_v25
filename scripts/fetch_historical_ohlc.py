#!/usr/bin/env python3
"""
Fetch IG historical OHLC into data/replay/ for offline signal replay.

  PYTHONPATH=src python3 scripts/fetch_historical_ohlc.py
  PYTHONPATH=src python3 scripts/fetch_historical_ohlc.py --points 500 --out data/replay/japan225_5m.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.config_loader import ConfigLoader
from system.credentials_loader import try_load_credentials
from system.ig_rest_session import ensure_shared_authenticated
from system.paths import data_dir, project_root
from trading.instrument_registry import InstrumentRegistry


def _default_epic(cfg_path: Path) -> tuple[str, str]:
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    reg = InstrumentRegistry(raw)
    enabled = reg.get_enabled()
    if enabled:
        return str(enabled[0].get("name") or "Market"), str(enabled[0].get("epic") or "")
    return str(raw.get("market_search") or "Market"), str(raw.get("epic") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch IG OHLC history for replay")
    parser.add_argument("--config", type=Path, default=project_root() / "config" / "config_v25.json")
    parser.add_argument("--epic", default="")
    parser.add_argument("--market", default="")
    parser.add_argument("--resolution", default="MINUTE_5")
    parser.add_argument("--points", type=int, default=288)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path (default: src/data/replay/<epic>_<resolution>_<points>.json)",
    )
    args = parser.parse_args()

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        print(f"FAIL: credentials — {status.error}", file=sys.stderr)
        return 1

    market, epic = args.market, args.epic
    if not epic:
        market, epic = _default_epic(args.config)
    if not epic:
        print("FAIL: no epic (set --epic or enable instrument in config)", file=sys.stderr)
        return 1

    rest = ensure_shared_authenticated(status.credentials)
    bars = rest.fetch_price_history(epic, resolution=args.resolution, num_points=args.points)
    if not bars:
        print(f"FAIL: no bars returned for {epic}", file=sys.stderr)
        return 1

    out = args.out
    if out is None:
        safe_epic = epic.replace(".", "_")
        out = data_dir() / "replay" / f"{safe_epic}_{args.resolution}_{args.points}.json"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market": market or epic,
        "epic": epic,
        "resolution": args.resolution,
        "num_points": args.points,
        "bar_count": len(bars),
        "bars": bars,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"OK: wrote {len(bars)} bars → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
