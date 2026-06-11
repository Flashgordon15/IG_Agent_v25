"""Build flat feature tables from feeder lake events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from expectancy.shadow_attribution import _direction_from_payload
from ingest.lake_reader import iter_events


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def features_dir(day: str) -> Path:
    d = _project_root() / "data_lake" / "features" / day
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flatten_signal(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload") or {}
    return {
        "ts": row.get("ts"),
        "epic": row.get("epic"),
        "market": row.get("market"),
        "session": row.get("session"),
        "direction": p.get("direction"),
        "raw_score": p.get("raw_score"),
        "adjusted_score": p.get("adjusted_score"),
        "would_fire": p.get("would_fire"),
        "setup_key": p.get("setup_key"),
        "ml_probability": p.get("ml_probability"),
        "gates_passed_n": len(p.get("gates_passed") or []),
        "risk_band": p.get("risk_band"),
        "pilot": p.get("pilot"),
        "pass_70": (p.get("threshold_pass") or {}).get(">=70"),
        "pass_75": (p.get("threshold_pass") or {}).get(">=75"),
        "pass_80": (p.get("threshold_pass") or {}).get(">=80"),
        "pass_85": (p.get("threshold_pass") or {}).get(">=85"),
    }


def _flatten_fill(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload") or {}
    return {
        "ts": row.get("ts"),
        "epic": row.get("epic"),
        "market": row.get("market"),
        "direction": _direction_from_payload(p) or None,
        "setup_key": p.get("setup_key"),
        "pnl_gbp": p.get("pnl_gbp"),
        "pnl_points": p.get("pnl_points"),
        "result": p.get("result"),
        "exit_reason": p.get("exit_reason"),
        "confidence": p.get("confidence"),
    }


def _flatten_bar(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload") or {}
    return {
        "ts": row.get("ts"),
        "epic": row.get("epic"),
        "market": row.get("market"),
        "session": row.get("session"),
        "bar_time": p.get("bar_time"),
        "open": p.get("open"),
        "high": p.get("high"),
        "low": p.get("low"),
        "close": p.get("close"),
        "volume": p.get("volume"),
    }


def build_day(day: str) -> dict[str, Path]:
    signals: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    bars: list[dict[str, Any]] = []
    for row in iter_events(day=day):
        et = str(row.get("event_type") or "")
        if et == "signal_eval":
            signals.append(_flatten_signal(row))
        elif et == "fill_close":
            fills.append(_flatten_fill(row))
        elif et == "bar_close":
            bars.append(_flatten_bar(row))

    out_dir = features_dir(day)
    written: dict[str, Path] = {}
    for name, rows in (
        ("signals", signals),
        ("fills", fills),
        ("bars", bars),
    ):
        path = out_dir / f"{name}.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        written[name] = path
    meta = out_dir / "meta.json"
    meta.write_text(
        json.dumps(
            {
                "day": day,
                "signals": len(signals),
                "fills": len(fills),
                "bars": len(bars),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    written["meta"] = meta
    return written
