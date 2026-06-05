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
from pathlib import Path
from typing import Any

import pandas as pd

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


def _replay_batch(
    *,
    epic: str,
    market: str,
    bars: list[dict],
    cfg: Any,
    stop_pts: float,
    threshold: float,
) -> tuple[list[dict], dict]:
    """Vectorised batch replay — precomputes all indicators once over the full
    OHLC series rather than rebuilding DataFrames bar-by-bar.

    Replicates the exact signal_engine.evaluate() scoring formula so that
    'fired' counts match live engine behaviour.
    """
    empty_summary = {
        "epic": epic,
        "market": market,
        "cache": "",
        "bars": len(bars),
        "records": 0,
        "fired": 0,
        "labels_3": {},
        "threshold": threshold,
        "stop_pts": stop_pts,
    }
    if not bars:
        return [], empty_summary

    # ── Build tidy 5m DataFrame ──────────────────────────────────────────────
    rows = []
    for b in bars:
        try:
            t = _parse_bar_time(str(b.get("t") or ""))
            c = float(b.get("c") or 0)
            h = float(b.get("h") or c)
            lo = float(b.get("l") or c)
            sp = float(b.get("spread") or max(1.0, h - lo))
            if c <= 0:
                continue
            rows.append(
                {
                    "time": t,
                    "open": float(b.get("o") or c),
                    "high": h,
                    "low": lo,
                    "close": c,
                    "spread": sp,
                }
            )
        except (TypeError, ValueError):
            continue
    if not rows:
        return [], empty_summary

    df5 = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)

    # ── Config parameters ────────────────────────────────────────────────────
    fast_span = int(getattr(cfg, "fast_ema", 9))
    slow_span = int(getattr(cfg, "slow_ema", 21))
    rsi_period = int(getattr(cfg, "rsi_period", 14))
    atr_period = int(getattr(cfg, "atr_period", 14))
    min_atr = float(getattr(cfg, "min_atr_points", 0))
    max_atr = float(getattr(cfg, "max_atr_points", 0))
    momentum_gap = max(float(getattr(cfg, "momentum_gap_points", 1)), 1)
    max_spread = float(getattr(cfg, "max_spread_points", 0))
    rsi_buy_min = float(getattr(cfg, "rsi_buy_min", 45))
    rsi_buy_max = float(getattr(cfg, "rsi_buy_max", 75))
    rsi_sell_min = float(getattr(cfg, "rsi_sell_min", 25))
    rsi_sell_max = float(getattr(cfg, "rsi_sell_max", 55))
    vol_filter = bool(getattr(cfg, "vol_regime_filter_enabled", False))

    # ── Add indicators (vectorised, one pass) ───────────────────────────────
    def _add_ind(d: pd.DataFrame) -> pd.DataFrame:
        d = d.copy()
        p = d["close"]
        d["fast_ema"] = p.ewm(span=fast_span, adjust=False).mean()
        d["slow_ema"] = p.ewm(span=slow_span, adjust=False).mean()
        delta = p.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        d["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
        pc = d["close"].shift(1)
        tr = pd.concat(
            [
                (d["high"] - d["low"]).abs(),
                (d["high"] - pc).abs(),
                (d["low"] - pc).abs(),
            ],
            axis=1,
        ).max(axis=1)
        d["atr"] = tr.ewm(alpha=1 / atr_period, adjust=False).mean().fillna(0)
        return d

    df5 = _add_ind(df5)

    # Precompute 15m candles + indicators, then merge onto 5m with asof join.
    df5_ts = df5.set_index("time")
    df15_ts = (
        df5_ts.resample("15min")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            spread=("spread", "last"),
        )
        .dropna(subset=["close"])
    )
    df15 = _add_ind(df15_ts.reset_index())
    # Shift 15m indicators by 1 bar to get "last closed 15m bar" at each 5m time.
    df15_shifted = df15.copy()
    for col in ("fast_ema", "slow_ema", "rsi", "atr"):
        df15_shifted[col] = df15[col].shift(1)
    df15_shifted = df15_shifted.rename(
        columns={
            "fast_ema": "fast15",
            "slow_ema": "slow15",
            "rsi": "rsi15",
            "atr": "atr15",
        }
    )[["time", "fast15", "slow15", "rsi15"]]

    # merge_asof: for each 5m bar, attach the most recent preceding 15m indicator row.
    df5_ts_col = df5[["time"]].copy()
    merged = pd.merge_asof(
        df5_ts_col.sort_values("time"),
        df15_shifted.sort_values("time"),
        on="time",
        direction="backward",
    )
    df5 = df5.merge(merged, on="time", how="left")

    # Pre-compute rolling vol_regime (vectorised percentile approach).
    atr_s = df5["atr"]
    roll_lo = atr_s.rolling(100, min_periods=10).quantile(0.25)
    roll_hi = atr_s.rolling(100, min_periods=10).quantile(0.75)
    regime_arr = pd.Series("unknown", index=df5.index)
    valid = atr_s.notna() & roll_lo.notna()
    regime_arr[valid & (atr_s <= roll_lo)] = "low"
    regime_arr[valid & (atr_s >= roll_hi)] = "high"
    regime_arr[valid & (atr_s > roll_lo) & (atr_s < roll_hi)] = "normal"

    # ── Exact scoring formula from signal_engine.evaluate() ─────────────────
    # Use signal_engine indexing: bar i is "current open bar"; last closed = i-1.
    records: list[dict] = []
    fired_count = 0
    labels_3: dict[str, int] = {"WIN": 0, "LOSS": 0, "BREAKEVEN": 0}
    WARMUP = max(slow_span + 4, rsi_period + 4, 6)

    for i in range(WARMUP, len(df5) - 1):
        last = df5.iloc[i - 1]  # last closed 5m bar
        prev = df5.iloc[i - 2]
        prev2 = df5.iloc[i - 3]

        atr_val = float(last["atr"])
        rsi_val = float(last["rsi"])
        spread = float(last["spread"])
        fast = float(last["fast_ema"])
        slow_val = float(last["slow_ema"])
        fast15 = float(last.get("fast15") or fast)
        slow15 = float(last.get("slow15") or slow_val)
        rsi15 = float(last.get("rsi15") or rsi_val)

        # ATR / vol gates (same as evaluate()).
        atr_ok = not (max_atr > 0 and atr_val > max_atr)
        vol_blocked = False
        current_regime = str(regime_arr.iloc[i - 1])
        if max_atr > 0 and atr_val > max_atr:
            vol_blocked = True
        if vol_filter and current_regime == "low":
            vol_blocked = True

        trend_gap = abs(fast - slow_val)
        momentum_bonus = min(10.0, trend_gap / momentum_gap * 10)
        bull_momentum = momentum_bonus if fast > slow_val else 0.0
        bear_momentum = momentum_bonus if fast < slow_val else 0.0

        rsi_buy_cap = rsi_buy_max if rsi_buy_max > rsi_buy_min else 99.0
        rsi_sell_cap = rsi_sell_min if rsi_sell_min < rsi_sell_max else 0.0

        spread_score = (
            max(0, min(20, 20 * (1 - spread / max(max_spread, 0.01))))
            if max_spread > 0
            else 0.0
        )

        two_bull = float(last["close"]) >= float(last["open"]) and float(
            prev["close"]
        ) >= float(prev["open"])
        two_bear = float(last["close"]) <= float(last["open"]) and float(
            prev["close"]
        ) <= float(prev["open"])
        price_up = float(last["close"]) >= float(prev["close"]) >= float(prev2["close"])
        price_down = (
            float(last["close"]) <= float(prev["close"]) <= float(prev2["close"])
        )

        buy = (
            (30 if fast15 >= slow15 and rsi15 >= 50 else 0)
            + (20 if fast > slow_val else 0)
            + (
                min(20, max(0, min(rsi_val, rsi_buy_cap) - rsi_buy_min))
                if rsi_val >= rsi_buy_min
                else 0
            )
            + (10 if price_up else 0)
            + spread_score
            + (10 if two_bull else 0)
            + bull_momentum
        )
        sell = (
            (30 if fast15 <= slow15 and rsi15 <= 50 else 0)
            + (20 if fast < slow_val else 0)
            + (
                min(20, max(0, rsi_sell_max - max(rsi_val, rsi_sell_cap)))
                if rsi_val <= rsi_sell_max
                else 0
            )
            + (10 if price_down else 0)
            + spread_score
            + (10 if two_bear else 0)
            + bear_momentum
        )

        if vol_blocked:
            buy *= 0.5
            sell *= 0.5
        if not atr_ok:
            buy *= 0.65
            sell *= 0.65
        if max_spread > 0 and spread > max_spread:
            buy *= 0.50
            sell *= 0.50

        raw_conf = max(buy, sell)
        raw_sig = "BUY" if buy > sell else "SELL" if sell > buy else "WAIT"

        buy_ok = buy >= threshold
        sell_ok = sell >= threshold
        if buy_ok and sell_ok:
            direction = "BUY" if buy >= sell else "SELL"
        elif buy_ok:
            direction = "BUY"
        elif sell_ok:
            direction = "SELL"
        else:
            direction = "WAIT"

        # RSI block (same as evaluate()).
        rsi_block = False
        if direction == "BUY" and rsi_buy_max > 0 and rsi_val > rsi_buy_max:
            rsi_block = True
        if direction == "SELL" and rsi_sell_min > 0 and rsi_val < rsi_sell_min:
            rsi_block = True

        meets_threshold = direction in ("BUY", "SELL") and not rsi_block
        meets_analysis = raw_conf >= 50.0

        if not (meets_threshold or meets_analysis):
            continue

        entry = float(last["close"])
        fh3, fl3, _ = _forward_extremes(bars, i - 1, 3)
        fh6, fl6, _ = _forward_extremes(bars, i - 1, 6)
        if fh3 <= 0:
            continue

        fired = bool(meets_threshold)
        if fired:
            fired_count += 1

        label_3 = _label_direction(
            direction, entry, fwd_high=fh3, fwd_low=fl3, stop_pts=stop_pts
        )
        label_6 = _label_direction(
            direction, entry, fwd_high=fh6, fwd_low=fl6, stop_pts=stop_pts
        )
        if fired:
            labels_3[label_3] = labels_3.get(label_3, 0) + 1

        ts = str(
            last["time"].isoformat()
            if hasattr(last["time"], "isoformat")
            else last["time"]
        )
        records.append(
            {
                "timestamp": ts,
                "epic": epic,
                "market": market,
                "direction": direction,
                "raw_score": round(raw_conf, 1),
                "adjusted_score": round(raw_conf, 1),
                "rsi": round(rsi_val, 1),
                "atr": round(atr_val, 4),
                "spread": round(spread, 4),
                "vol_regime": current_regime,
                "setup_key": f"{direction}|{'bull' if fast > slow_val else 'bear'}|vol{current_regime}",
                "fired": fired,
                "label_3": label_3,
                "label_6": label_6,
                "atr_ratio": round(atr_val / stop_pts, 4) if stop_pts > 0 else 0.0,
                "rsi_block": rsi_block,
                "entry": entry,
                "fwd_high_3": fh3,
                "fwd_low_3": fl3,
                "fwd_high_6": fh6,
                "fwd_low_6": fl6,
            }
        )

    summary = {
        "epic": epic,
        "market": market,
        "cache": "",
        "bars": len(bars),
        "records": len(records),
        "fired": fired_count,
        "labels_3": labels_3,
        "threshold": threshold,
        "stop_pts": stop_pts,
    }
    return records, summary


def _config_for_instrument(base_cfg: Any, inst: dict[str, Any]) -> Any:

    from runtime.agent_bootstrap import _config_for_instrument as _overlay

    return _overlay(base_cfg, inst)


def replay_one(
    *,
    epic: str,
    market: str,
    cache_path: Path,
    base_cfg: Any,
    inst: dict[str, Any],
    window: int = 300,
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
    # Override max_live_quotes with the replay window to keep DataFrame small.
    # The live value (3000) is designed for streaming; 300 bars is plenty for
    # EMA/RSI/ATR (longest period is ~26 bars).
    cfg._data["max_live_quotes"] = max(window, 50)
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
    parser.add_argument(
        "--all", action="store_true", help="Replay all enabled instruments"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to replay_results.jsonl (default: overwrite on first, append on --all after first)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=300,
        help="Sliding quote window size for replay (default: 300, overrides max_live_quotes). "
        "Smaller = faster; 300 is enough for EMA/RSI/ATR calculations.",
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
        bars = _load_bars(cache_path)
        print(f"Replaying {market} ({epic})  bars={len(bars)} …", flush=True)

        cfg = _config_for_instrument(base_cfg, inst)
        threshold = float(cfg.signal_threshold)
        stop_pts = float(getattr(cfg, "stop_distance_points", 45) or 45)

        records, summary = _replay_batch(
            epic=epic,
            market=market,
            bars=bars,
            cfg=cfg,
            stop_pts=stop_pts,
            threshold=threshold,
        )
        summary["cache"] = str(cache_path)

        mode = "a" if OUTPUT_PATH.stat().st_size > 0 else "w"
        with OUTPUT_PATH.open(mode, encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        all_summaries.append(summary)
        print(
            f"  -> signals={summary['records']}  fired={summary['fired']}  "
            f"labels={summary['labels_3']}",
            flush=True,
        )
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
