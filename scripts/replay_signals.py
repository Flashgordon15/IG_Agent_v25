#!/usr/bin/env python3
"""
Replay SignalEngine.evaluate() over historical OHLC (walk-forward).

  PYTHONPATH=src python3 scripts/replay_signals.py --input src/data/replay/japan225.json
  PYTHONPATH=src python3 scripts/replay_signals.py --input bars.json --out src/data/replay/signals.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from signals.signal_engine import SignalEngine
from system.config_loader import ConfigLoader
from trading.ohlc_bootstrap import _parse_bar_time


def _bar_to_quote(bar: dict) -> Quote | None:
    high = float(bar.get("high") or 0)
    low = float(bar.get("low") or 0)
    if high <= 0 or low <= 0:
        return None
    mid = (high + low) / 2.0
    bid_close = float(bar.get("bid_close") or 0)
    offer_close = float(bar.get("offer_close") or 0)
    if bid_close > 0 and offer_close > bid_close:
        bid, offer = bid_close, offer_close
    else:
        spread = max(1.0, float(bar.get("close") or mid) * 0.0001)
        bid = mid - spread / 2.0
        offer = mid + spread / 2.0
    return Quote(time=_parse_bar_time(bar.get("time", "")), bid=bid, offer=offer)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay signals on historical OHLC")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config_v25.json")
    parser.add_argument("--input", type=Path, required=True, help="JSON from fetch_historical_ohlc.py")
    parser.add_argument("--market", default="", help="Override market key (default: from file)")
    parser.add_argument("--out", type=Path, default=None, help="JSONL output path")
    parser.add_argument("--warmup-bars", type=int, default=4, help="Min 5m candles before evaluate")
    args = parser.parse_args()

    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    bars = raw.get("bars") if isinstance(raw, dict) else raw
    if not isinstance(bars, list) or not bars:
        print("FAIL: input has no bars[]", file=sys.stderr)
        return 1

    market = args.market or str(raw.get("market") or "Market")
    cfg = ConfigLoader(args.config).load_config()
    engine = SignalEngine(cfg)

    out = args.out or Path(args.input).with_suffix(".signals.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    threshold = float(cfg.signal_threshold)
    rows: list[dict] = []

    for i, bar in enumerate(bars):
        quote = _bar_to_quote(bar)
        if quote is None:
            continue
        engine.add_quote(market, quote)
        df = engine.quote_df(market)
        c5 = engine.candles(df, 5)
        if len(c5) < args.warmup_bars:
            continue
        sig = engine.evaluate(market)
        snap = sig.snapshot or {}
        last = snap.get("last")
        rsi = float(last.get("rsi", 0)) if last is not None and hasattr(last, "get") else None
        rows.append(
            {
                "index": i,
                "time": quote.time.isoformat(),
                "signal": sig.signal,
                "raw_confidence": round(float(sig.raw_confidence), 2),
                "adjusted_confidence": round(float(sig.adjusted_confidence), 2),
                "threshold": threshold,
                "setup_key": sig.setup_key,
                "rsi": round(rsi, 2) if rsi is not None else None,
                "rsi_block": snap.get("rsi_block"),
                "buy_score": snap.get("buy_score"),
                "sell_score": snap.get("sell_score"),
                "notes": sig.notes[:500],
            }
        )

    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    print(
        f"OK: {len(rows)} evaluations from {len(bars)} bars → {out} "
        f"(market={market!r}, threshold={threshold})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
