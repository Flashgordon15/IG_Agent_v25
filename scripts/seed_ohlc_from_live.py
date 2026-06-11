#!/usr/bin/env python3
"""
Seed OHLC JSONL cache from live IG prices when historical allowance is blocked.

WARNING: Synthetic bars must NOT be used in production paths. Output must be renamed
to *.jsonl.synthetic (never left as *.jsonl) so ohlc_cache_paths / signal engine
cannot load them. Use fetch_historical_ohlc.py for real data only.

Uses current bid/offer to synthesize MINUTE_5 bars for signal warmup/replay.
Not a substitute for fetch_historical_ohlc.py — rerun full fetch when allowance resets.

  PYTHONPATH=src python3 scripts/seed_ohlc_from_live.py --epic CS.D.EURUSD.CFD.IP --market "EUR/USD"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.credentials_loader import try_load_credentials
from system.engine_log import log_engine
from system.ig_rest_session import ensure_shared_authenticated
from trading.ohlc_cache_paths import ohlc_cache_path

LONDON = ZoneInfo("Europe/London")
DEFAULT_BARS = 600


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed OHLC cache from live IG prices")
    p.add_argument("--epic", required=True)
    p.add_argument("--market", default="")
    p.add_argument("--bars", type=int, default=DEFAULT_BARS)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    epic = str(args.epic).strip()
    market = str(args.market or epic)
    n = max(50, int(args.bars))

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        print(f"FAIL: credentials — {status.error}", file=sys.stderr)
        return 1

    rest = ensure_shared_authenticated(status.credentials)
    rest.ensure_session()
    c = rest.fetch_market_constraints(epic)
    bid = float(c.get("bid") or 0)
    offer = float(c.get("offer") or 0)
    if bid <= 0 or offer <= 0:
        bid, offer = rest.fetch_live_prices(epic)
    if bid <= 0 or offer <= 0:
        print(f"FAIL: no live price for {epic}", file=sys.stderr)
        return 1

    mid = (bid + offer) / 2.0
    spread = max(offer - bid, mid * 0.00005 if mid < 10 else 1.0)
    # Bar range scales with instrument price level
    bar_rng = spread * 3 if mid < 10 else max(spread * 2, mid * 0.0002)

    cache_path = ohlc_cache_path(epic, market=market)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    end = datetime.now(LONDON).replace(tzinfo=None)
    rows: list[dict] = []
    price = mid
    for i in range(n):
        t = end - timedelta(minutes=5 * (n - 1 - i))
        drift = bar_rng * (0.2 if i % 2 == 0 else -0.15)
        o = price
        c = price + drift
        h = max(o, c) + bar_rng * 0.3
        low = min(o, c) - bar_rng * 0.3
        price = c
        rows.append(
            {
                "t": t.strftime("%Y-%m-%dT%H:%M:%S"),
                "o": round(o, 5 if mid < 10 else 2),
                "h": round(h, 5 if mid < 10 else 2),
                "l": round(low, 5 if mid < 10 else 2),
                "c": round(c, 5 if mid < 10 else 2),
                "v": 0,
                "spread": round(spread, 5 if mid < 10 else 2),
            }
        )

    with cache_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    msg = (
        f"seed_ohlc_from_live: wrote {len(rows)} synthetic 5m bars for {epic} "
        f"(mid={mid:.5g} spread={spread:.5g}) -> {cache_path}"
    )
    log_engine(msg)
    print(msg)
    print("NOTE: synthetic seed only — run fetch_historical_ohlc.py after allowance resets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
