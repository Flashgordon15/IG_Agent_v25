"""
Tune S2 _MIN_RANGE_PCT per epic from OHLC bar lab.

Picks the threshold whose would_trade rate is closest to a target rate per
asset class (indices, gold, FX).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.historical_bar_lab import (
    _bar_to_event,
    _load_bars,
    _load_enabled_markets,
    _session_for_bar,
)
from strategies.s2_momentum import S2Momentum


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _epic_class(epic: str) -> str:
    u = epic.upper()
    if "EURUSD" in u or "GBPUSD" in u:
        return "fx"
    if "GOLD" in u or "CFPGOLD" in u:
        return "gold"
    return "index"


_TARGET_WT_RATE = {
    "fx": 0.015,
    "gold": 0.06,
    "index": 0.04,
}

_SWEEP = [
    0.0003,
    0.0004,
    0.0005,
    0.0006,
    0.0008,
    0.001,
    0.0012,
    0.0015,
    0.002,
    0.0025,
]


def _count_would_trade(
    bars: list[dict[str, Any]],
    *,
    epic: str,
    market: str,
    min_range_pct: float,
) -> int:
    s2 = S2Momentum(min_range_pct=min_range_pct)
    wt = 0
    for bar in bars:
        session = _session_for_bar(str(bar.get("t") or ""))
        event = _bar_to_event(bar, epic=epic, market=market, session=session)
        intent = s2.evaluate_feeder_event(event)
        if intent and intent.would_trade:
            wt += 1
    return wt


def tune_epic(
    *,
    epic: str,
    market: str,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    n_bars = len(bars)
    asset_class = _epic_class(epic)
    target = _TARGET_WT_RATE[asset_class]
    curve: list[dict[str, Any]] = []
    best_thr = 0.0008
    best_dist = 1e9
    best_wt = 0
    for thr in _SWEEP:
        wt = _count_would_trade(bars, epic=epic, market=market, min_range_pct=thr)
        rate = wt / n_bars if n_bars else 0.0
        dist = abs(rate - target)
        curve.append(
            {
                "min_range_pct": thr,
                "would_trade": wt,
                "rate": round(rate, 4),
                "target_rate": target,
            }
        )
        if dist < best_dist:
            best_dist = dist
            best_thr = thr
            best_wt = wt
    return {
        "epic": epic,
        "market": market,
        "asset_class": asset_class,
        "bars": n_bars,
        "target_wt_rate": target,
        "min_range_pct": best_thr,
        "would_trade": best_wt,
        "would_trade_rate": round(best_wt / n_bars, 4) if n_bars else 0.0,
        "curve": curve,
    }


def run_s2_threshold_tune() -> dict[str, Any]:
    _ensure_ohlc_paths()
    results: list[dict[str, Any]] = []
    for _iid, epic, market in _load_enabled_markets():
        from trading.ohlc_cache_paths import ohlc_cache_path

        cache = ohlc_cache_path(epic, market=market)
        bars = _load_bars(cache)
        if not bars:
            continue
        results.append(tune_epic(epic=epic, market=market, bars=bars))
    by_epic = {r["epic"]: r for r in results}
    return {
        "ok": bool(results),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "default_min_range_pct": 0.0008,
        "by_epic": by_epic,
        "markets": len(results),
    }


def _ensure_ohlc_paths() -> None:
    import sys

    src = _project_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def write_s2_threshold_snapshot() -> Path:
    payload = run_s2_threshold_tune()
    out_dir = _project_root() / "data_lake" / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "s2_epic_thresholds.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
