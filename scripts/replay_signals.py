#!/usr/bin/env python3
"""
Replay walk-forward signals for one OHLC cache (multi-market Phase A).

  PYTHONPATH=src python3 scripts/replay_signals.py --epic CS.D.EURUSD.CFD.IP --market "EUR/USD"
  PYTHONPATH=src python3 scripts/replay_signals.py --all
"""

from __future__ import annotations

import argparse
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
from trading.ohlc_cache_paths import ohlc_cache_path

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


def _config_for_instrument(base_cfg: Any, inst: dict[str, Any]) -> Any:
    from copy import deepcopy

    from runtime.agent_bootstrap import _config_for_instrument as _overlay

    return _overlay(base_cfg, inst)


def replay_one(
    *,
    epic: str,
    market: str,
    cache_path: Path,
    base_cfg: Any,
    inst: dict[str, Any],
) -> tuple[list[dict], dict[str, Any]]:
    bars = _load_bars(cache_path)
    summary: dict[str, Any] = {
        "epic": epic,
        "market": market,
        "cache": str(cache_path),
        "bars": len(bars),
        "records": 0,
        "fired": 0,
        "labels_3": {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0},
    }
    if not bars:
        return [], summary

    cfg = _config_for_instrument(base_cfg, inst)
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
        records.append(
            {
                "timestamp": ts,
                "epic": epic,
                "market": market,
                "direction": direction,
                "raw_score": round(raw_score, 1),
                "adjusted_score": round(adj_score, 1),
                "rsi": round(rsi_val, 1) if rsi_val is not None else None,
                "atr": round(atr_val, 4) if atr_val is not None else None,
                "spread": round(float(bar.get("spread") or quote.spread), 4),
                "vol_regime": regime,
                "setup_key": sig.setup_key,
                "fired": fired,
                "forward_high_3": round(fh3, 4),
                "forward_low_3": round(fl3, 4),
                "forward_close_3": round(fc3, 4),
                "forward_high_6": round(fh6, 4),
                "forward_low_6": round(fl6, 4),
                "forward_close_6": round(fc6, 4),
                "label_3bar": label_3,
                "label_6bar": label_6,
                "stop_pts": stop_pts,
                "session_window": session_name(quote.time),
            }
        )

    summary.update(
        records=len(records),
        fired=fired_count,
        labels_3=labels_3,
        threshold=threshold,
        stop_pts=stop_pts,
    )
    return records, summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward signal replay")
    parser.add_argument("--epic", default="", help="IG epic")
    parser.add_argument("--market", default="", help="Market display name")
    parser.add_argument("--all", action="store_true", help="Replay all enabled instruments")
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to replay_results.jsonl (default: overwrite on first, append on --all after first)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cfg_path = ROOT / "config" / "config_v25.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    reg = InstrumentRegistry(raw)
    base_cfg = ConfigLoader(cfg_path).load_config()

    jobs: list[tuple[str, str, dict[str, Any]]] = []
    if args.all:
        for iid, inst in reg.get_enabled_with_ids():
            epic = str(inst.get("epic") or "")
            market = str(inst.get("name") or iid)
            jobs.append((epic, market, inst))
    elif args.epic:
        epic = str(args.epic).strip()
        inst = reg.get_by_epic(epic) or {}
        market = str(args.market or inst.get("name") or epic)
        jobs.append((epic, market, inst))
    else:
        enabled = reg.get_enabled()
        if not enabled:
            print("No enabled instruments", file=sys.stderr)
            return 1
        inst = enabled[0]
        epic = str(inst.get("epic") or raw.get("epic") or "")
        market = str(inst.get("name") or "Japan 225")
        jobs.append((epic, market, inst))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not args.append and not args.all:
        OUTPUT_PATH.write_text("", encoding="utf-8")
    elif args.all:
        OUTPUT_PATH.write_text("", encoding="utf-8")

    all_summaries: list[dict[str, Any]] = []
    for epic, market, inst in jobs:
        cache_path = ohlc_cache_path(epic, market=market)
        records, summary = replay_one(
            epic=epic,
            market=market,
            cache_path=cache_path,
            base_cfg=base_cfg,
            inst=inst,
        )
        mode = "a" if OUTPUT_PATH.stat().st_size > 0 else "w"
        with OUTPUT_PATH.open(mode, encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        all_summaries.append(summary)
        print(f"--- {market} ({epic}) ---")
        print(f"Cache: {cache_path} bars={summary['bars']}")
        print(f"Signals recorded: {summary['records']} fired: {summary['fired']}")
        if summary["bars"] == 0:
            print("  (no bars — run fetch_historical_ohlc.py for this epic)")

    print("\n=== REPLAY SUMMARY ===")
    total_records = sum(int(s.get("records") or 0) for s in all_summaries)
    total_fired = sum(int(s.get("fired") or 0) for s in all_summaries)
    print(f"Markets: {len(all_summaries)}")
    print(f"Total signals recorded: {total_records}")
    print(f"Total fired: {total_fired}")
    print(f"Output: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
