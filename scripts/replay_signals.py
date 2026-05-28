#!/usr/bin/env python3
"""
Walk-forward signal replay on OHLC cache using the real SignalEngine.

  PYTHONPATH=src python3 scripts/replay_signals.py

Reads:  src/data/ohlc_cache/nikkei_5m.jsonl
Writes: src/data/replay_results.jsonl
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from signals.indicators import session_name, vol_regime
from signals.signal_engine import SignalEngine
from system.config_loader import ConfigLoader
from trading.instrument_registry import InstrumentRegistry
from trading.ohlc_bootstrap import _parse_bar_time

CACHE_PATH = ROOT / "src" / "data" / "ohlc_cache" / "nikkei_5m.jsonl"
OUTPUT_PATH = ROOT / "src" / "data" / "replay_results.jsonl"
WARMUP_BARS = 4


def _load_bars(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    bars: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            bars.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    bars.sort(key=lambda b: str(b.get("t") or ""))
    return bars


def _bar_to_quote(bar: dict) -> Quote | None:
    try:
        c = float(bar.get("c") or 0)
        h = float(bar.get("h") or 0)
        low = float(bar.get("l") or 0)
        if c <= 0 or h <= 0 or low <= 0:
            return None
        spread = float(bar.get("spread") or max(1.0, h - low))
        bid = c - spread / 2.0
        offer = c + spread / 2.0
        t = _parse_bar_time(str(bar.get("t") or ""))
        return Quote(time=t, bid=bid, offer=offer)
    except (TypeError, ValueError):
        return None


def _forward_extremes(bars: list[dict], idx: int, n: int) -> tuple[float, float, float]:
    """High, low, close over the next *n* bars after *idx*."""
    window = bars[idx + 1 : idx + 1 + n]
    if not window:
        return 0.0, 0.0, 0.0
    highs = [float(b.get("h") or b.get("c") or 0) for b in window]
    lows = [float(b.get("l") or b.get("c") or 0) for b in window]
    closes = [float(b.get("c") or 0) for b in window]
    return max(highs), min(lows), closes[-1]


def _label_direction(
    direction: str,
    entry: float,
    *,
    fwd_high: float,
    fwd_low: float,
    stop_pts: float,
) -> str:
    if entry <= 0 or stop_pts <= 0:
        return "BREAKEVEN"
    if direction == "SELL":
        win = fwd_low <= entry - stop_pts
        loss = fwd_high >= entry + stop_pts
    elif direction == "BUY":
        win = fwd_high >= entry + stop_pts
        loss = fwd_low <= entry - stop_pts
    else:
        return "BREAKEVEN"
    if win and not loss:
        return "WIN"
    if loss and not win:
        return "LOSS"
    return "BREAKEVEN"


def _resolve_market(cfg_path: Path) -> str:
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    reg = InstrumentRegistry(raw)
    enabled = reg.get_enabled()
    if enabled:
        return str(enabled[0].get("name") or "Japan 225")
    return str(raw.get("market_search") or "Japan 225")


def main() -> int:
    bars = _load_bars(CACHE_PATH)
    if not bars:
        print(f"No bars in {CACHE_PATH} — run fetch_historical_ohlc.py first (exiting 0)")
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text("", encoding="utf-8")
        print("Wrote empty replay_results.jsonl")
        return 0

    cfg_path = ROOT / "config" / "config_v25.json"
    cfg = ConfigLoader(cfg_path).load_config()
    market = _resolve_market(cfg_path)
    engine = SignalEngine(cfg)
    threshold = float(cfg.signal_threshold)
    stop_pts = float(getattr(cfg, "stop_distance_points", 45) or 45)

    records: list[dict] = []
    fired_count = 0
    labels_3: dict[str, int] = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}

    for i, bar in enumerate(bars):
        quote = _bar_to_quote(bar)
        if quote is None:
            continue
        engine.add_quote(market, quote)
        df = engine.quote_df(market)
        c5 = engine.candles(df, 5)
        if len(c5) < WARMUP_BARS:
            continue

        sig = engine.evaluate(market)
        direction = str(sig.signal or "WAIT")
        raw_score = float(sig.raw_confidence)
        adj_score = float(sig.adjusted_confidence)
        snap = sig.snapshot or {}
        last = snap.get("last")
        rsi_val = None
        atr_val = None
        if last is not None and hasattr(last, "get"):
            try:
                rsi_val = float(last.get("rsi", 0))
                atr_val = float(last.get("atr", 0))
            except (TypeError, ValueError):
                pass

        score_for_gate = adj_score
        meets_threshold = direction in ("BUY", "SELL") and score_for_gate >= threshold
        meets_analysis = score_for_gate >= 50.0
        if not (meets_threshold or meets_analysis):
            continue

        entry = float(bar.get("c") or quote.mid)
        fh3, fl3, fc3 = _forward_extremes(bars, i, 3)
        fh6, fl6, fc6 = _forward_extremes(bars, i, 6)
        if fh3 <= 0:
            continue

        fired = bool(meets_threshold and not snap.get("rsi_block"))
        if fired:
            fired_count += 1

        atr_series = None
        c5i = engine.add_indicators(c5)
        if "atr" in c5i.columns:
            atr_series = c5i["atr"]
        regime = str(snap.get("vol_regime") or vol_regime(atr_series))

        label_3 = _label_direction(
            direction, entry, fwd_high=fh3, fwd_low=fl3, stop_pts=stop_pts
        )
        label_6 = _label_direction(
            direction, entry, fwd_high=fh6, fwd_low=fl6, stop_pts=stop_pts
        )
        if fired:
            labels_3[label_3] = labels_3.get(label_3, 0) + 1

        ts = str(bar.get("t") or quote.time.isoformat())
        row = {
            "timestamp": ts,
            "direction": direction,
            "raw_score": round(raw_score, 1),
            "adjusted_score": round(adj_score, 1),
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "atr": round(atr_val, 1) if atr_val is not None else None,
            "spread": round(float(bar.get("spread") or quote.spread), 1),
            "vol_regime": regime,
            "setup_key": sig.setup_key,
            "fired": fired,
            "forward_high_3": round(fh3, 1),
            "forward_low_3": round(fl3, 1),
            "forward_close_3": round(fc3, 1),
            "forward_high_6": round(fh6, 1),
            "forward_low_6": round(fl6, 1),
            "forward_close_6": round(fc6, 1),
            "label_3bar": label_3,
            "label_6bar": label_6,
            "stop_pts": stop_pts,
            "session_window": session_name(quote.time),
        }
        records.append(row)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row) + "\n")

    print("=== REPLAY SUMMARY ===")
    print(f"Bars processed: {len(bars)}")
    print(f"Signals recorded (score>=50): {len(records)}")
    print(f"Signals fired (score>={threshold}, no RSI block): {fired_count}")
    print(
        f"3-bar labels (fired only): WIN={labels_3.get('WIN', 0)} "
        f"LOSS={labels_3.get('LOSS', 0)} "
        f"BREAKEVEN={labels_3.get('BREAKEVEN', 0)}"
    )
    print(f"Output: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
