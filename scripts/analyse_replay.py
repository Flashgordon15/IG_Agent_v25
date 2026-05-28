#!/usr/bin/env python3
"""
Analyse replay_results.jsonl — threshold, RSI, session, vol regime report.

  PYTHONPATH=src python3 scripts/analyse_replay.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "src" / "data" / "replay_results.jsonl"
OUTPUT_PATH = ROOT / "src" / "data" / "replay_analysis.txt"

THRESHOLDS = [50, 60, 70, 75, 80, 85, 90]
RSI_SELL_RANGES = [
    (20, 45),
    (25, 45),
    (30, 45),
    (20, 40),
]
SESSION_WINDOWS = ("asia_early", "tokyo", "london_open")
VOL_REGIMES = ("low", "normal", "high", "unknown")


def _load_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", ""))
    except ValueError:
        return None


def _session_bucket(row: dict) -> str:
    ts = _parse_ts(str(row.get("timestamp") or ""))
    base = str(row.get("session_window") or "")
    if ts is None:
        return base or "unknown"
    hour = ts.hour
    if hour >= 23 or hour < 7:
        return "tokyo" if base == "asia_early" and hour >= 23 else base or "asia_early"
    if base == "london_morning":
        return "london_open"
    return base or "unknown"


def _stats_for_rows(rows: list[dict]) -> tuple[int, float, float]:
    """Return count, win_rate%, profit_factor (3-bar labels)."""
    if not rows:
        return 0, 0.0, 0.0
    wins = sum(1 for r in rows if r.get("label_3bar") == "WIN")
    losses = sum(1 for r in rows if r.get("label_3bar") == "LOSS")
    decided = wins + losses
    win_rate = (100.0 * wins / decided) if decided else 0.0
    stop = float(rows[0].get("stop_pts") or 45)
    gross_win = wins * stop
    gross_loss = losses * stop
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = 99.99
    else:
        pf = 0.0
    return len(rows), win_rate, pf


def _filter_threshold(rows: list[dict], thr: float) -> list[dict]:
    out = []
    for r in rows:
        if r.get("direction") not in ("BUY", "SELL"):
            continue
        if float(r.get("adjusted_score") or 0) < thr:
            continue
        if r.get("rsi_block"):
            continue
        out.append(r)
    return out


def _build_report(rows: list[dict], total_bars: int) -> str:
    lines: list[str] = []
    date_lo = ""
    date_hi = ""
    if rows:
        ts_sorted = sorted(str(r.get("timestamp") or "") for r in rows)
        date_lo = ts_sorted[0][:10]
        date_hi = ts_sorted[-1][:10]
    score50 = [r for r in rows if float(r.get("adjusted_score") or 0) >= 50]

    lines.append("=== JAPAN 225 SIGNAL REPLAY ANALYSIS ===")
    lines.append(f"Date range: {date_lo or 'n/a'} to {date_hi or 'n/a'}")
    lines.append(f"Total bars: {total_bars}")
    lines.append(f"Total signals (score>=50): {len(score50)}")
    lines.append("")
    lines.append("THRESHOLD ANALYSIS (3-bar forward):")
    lines.append("threshold  signals  win_rate  profit_factor")
    best_thr = 70
    best_pf = -1.0
    for thr in THRESHOLDS:
        subset = _filter_threshold(rows, thr)
        n, wr, pf = _stats_for_rows(subset)
        lines.append(f"{thr:<9}  {n:<7}  {wr:5.1f}%     {pf:4.2f}")
        if n >= 5 and pf > best_pf:
            best_pf = pf
            best_thr = thr

    lines.append("")
    lines.append("RSI SELL RANGE ANALYSIS:")
    lines.append("rsi_min  rsi_max  signals  win_rate")
    best_rsi = (20, 45)
    best_rsi_wr = -1.0
    sells = [r for r in rows if r.get("direction") == "SELL" and r.get("fired")]
    for lo, hi in RSI_SELL_RANGES:
        bucket = [
            r
            for r in sells
            if r.get("rsi") is not None and lo <= float(r["rsi"]) <= hi
        ]
        n, wr, _ = _stats_for_rows(bucket)
        lines.append(f"{lo:<7}  {hi:<7}  {n:<7}  {wr:5.1f}%")
        if n >= 3 and wr > best_rsi_wr:
            best_rsi_wr = wr
            best_rsi = (lo, hi)

    lines.append("")
    lines.append("SESSION WINDOW ANALYSIS:")
    lines.append("window        signals  win_rate  avg_score")
    session_best = "asia_early"
    session_best_wr = -1.0
    fired = [r for r in rows if r.get("fired")]
    for window in SESSION_WINDOWS:
        bucket = [r for r in fired if _session_bucket(r) == window]
        n, wr, _ = _stats_for_rows(bucket)
        avg = (
            sum(float(r.get("adjusted_score") or 0) for r in bucket) / n
            if n
            else 0.0
        )
        lines.append(f"{window:<13}  {n:<7}  {wr:5.1f}%     {avg:4.1f}")
        if n >= 3 and wr > session_best_wr:
            session_best_wr = wr
            session_best = window

    lines.append("")
    lines.append("VOL REGIME ANALYSIS:")
    lines.append("regime   signals  win_rate")
    for regime in VOL_REGIMES:
        bucket = [r for r in fired if str(r.get("vol_regime") or "") == regime]
        n, wr, _ = _stats_for_rows(bucket)
        lines.append(f"{regime:<8}  {n:<7}  {wr:5.1f}%")

    cfg_path = ROOT / "config" / "config_v25.json"
    cfg_raw: dict[str, Any] = {}
    if cfg_path.is_file():
        cfg_raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    lines.append("")
    lines.append("RECOMMENDED CONFIG:")
    lines.append(f"  signal_threshold: {best_thr}  (best profit factor)")
    lines.append(f"  rsi_sell_min: {best_rsi[0]}      (best win rate)")
    lines.append(f"  rsi_sell_max: {best_rsi[1]}")
    lines.append(f"  rsi_buy_min: {cfg_raw.get('rsi_buy_min', 58)}")
    lines.append(f"  rsi_buy_max: {cfg_raw.get('rsi_buy_max', 78)}")
    lines.append(f"  Best session: {session_best}")
    return "\n".join(lines) + "\n"


def main() -> int:
    rows = _load_rows(INPUT_PATH)
    cache = ROOT / "src" / "data" / "ohlc_cache" / "nikkei_5m.jsonl"
    total_bars = 0
    if cache.is_file():
        total_bars = sum(1 for line in cache.read_text(encoding="utf-8").splitlines() if line.strip())

    report = _build_report(rows, total_bars)
    print(report)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"Saved: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
