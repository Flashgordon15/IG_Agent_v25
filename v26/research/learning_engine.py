"""Unified v26 learning snapshot — gates, replay, shadow, bars, L1."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest.lake_reader import events_dir, utc_today
from research.bar_analyzer import analyze_bars, bar_report_to_dict
from research.cert_config import max_soak_days
from research.gate_blockers import build_gate_blocker_report, report_to_dict
from research.l1_certification import evaluate_l1
from research.l1_replay import replay_days
from research.shadow_expectancy import analyze_near_miss, near_miss_to_dict
from research.shadow_strategy_report import summarize_shadow_days
from research.trade_learning import build_trade_learning_report

try:
    from portfolio.allocator import PortfolioAllocator
except ImportError:
    PortfolioAllocator = None  # type: ignore[misc, assignment]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_trail_tune() -> dict[str, Any]:
    path = _project_root() / "data_lake" / "state" / "trail_epic_overrides.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_ohlc_replay() -> dict[str, Any]:
    path = _project_root() / "data_lake" / "state" / "v26_ohlc_replay.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def list_event_days(*, max_days: int | None = None) -> list[str]:
    cap = max_days if max_days is not None else max_soak_days()
    root = events_dir()
    if not root.is_dir():
        return []
    days = sorted(
        (p.stem for p in root.glob("*.jsonl") if p.is_file()),
        reverse=True,
    )
    return days[:cap]


def build_learning_snapshot(
    *,
    days: list[str] | None = None,
    max_days: int | None = None,
) -> dict[str, Any]:
    soak = max_days if max_days is not None else max_soak_days()
    day_list = days if days is not None else list_event_days(max_days=soak)
    if not day_list:
        day_list = [utc_today()]

    latest = day_list[0]
    gates = report_to_dict(build_gate_blocker_report(day=latest))
    bars = bar_report_to_dict(analyze_bars(day=latest))
    near_miss = near_miss_to_dict(analyze_near_miss(day=latest))
    replay = replay_days(day_list)
    shadow = summarize_shadow_days(day_list)
    l1 = evaluate_l1(day_list)
    ohlc = _load_ohlc_replay()
    trail_tune = _load_trail_tune()
    trade_learning = build_trade_learning_report(live_days=min(len(day_list) * 7, soak))
    allocator = (
        PortfolioAllocator.from_config().snapshot()
        if PortfolioAllocator is not None
        else {}
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_analyzed": len(day_list),
        "latest_day": latest,
        "l1_certification": l1,
        "l1_replay": replay,
        "gate_blockers_latest": gates,
        "bar_analysis_latest": bars,
        "near_miss_latest": near_miss,
        "shadow_summary": shadow,
        "ohlc_replay": ohlc,
        "trail_tune": trail_tune,
        "portfolio_envelope": allocator,
        "trade_learning": trade_learning,
        "learning_focus": _learning_focus(
            gates, bars, shadow, l1, ohlc, trade_learning, trail_tune
        ),
    }


def _learning_focus(
    gates: dict[str, Any],
    bars: dict[str, Any],
    shadow: dict[str, Any],
    l1: dict[str, Any],
    ohlc: dict[str, Any] | None = None,
    trade_learning: dict[str, Any] | None = None,
    trail_tune: dict[str, Any] | None = None,
) -> list[str]:
    tips: list[str] = []
    buckets = gates.get("confidence_buckets") or {}
    band_60_79 = sum(int(buckets.get(k) or 0) for k in ("60-69", "70-74", "75-79"))
    if band_60_79 > 1000:
        tips.append(
            f"Confidence 60–79% dominates ({band_60_79} evals) — "
            "v26 shadow is primary learning path until threshold met."
        )
    if bars.get("s2_would_trade", 0) == 0 and bars.get("total_bars", 0) > 10:
        tips.append(
            "S2 momentum: zero would_trade — review bar range thresholds (bar_analyzer)."
        )
    s3 = (shadow.get("by_strategy") or {}).get("S3_session_fx", {})
    if int(s3.get("would_trade") or 0) > 0:
        tips.append(
            f"S3 FX active ({s3.get('would_trade')} would_trade) — "
            "compare vs S1 on same sessions."
        )
    if l1.get("days_available", 0) < l1.get("days_required", max_soak_days()):
        tips.append(
            f"L1 cert: {l1.get('days_available')}/{l1.get('days_required')} days — "
            "keep feeder running daily."
        )
    ohlc = ohlc or {}
    bar_lab = ohlc.get("bar_lab") or {}
    s2_hist = (bar_lab.get("by_strategy") or {}).get("S2_momentum", {})
    if int(s2_hist.get("would_trade") or 0) > 0:
        tips.append(
            f"OHLC history: S2 would_trade {s2_hist.get('would_trade')} "
            f"over {bar_lab.get('total_bars')} bars — tune live S2 vs replay."
        )
    for hint in (ohlc.get("ml_veto_hints") or [])[:2]:
        tips.append(f"Walk-forward: {hint}")
    tl = trade_learning or {}
    for tip in (tl.get("learning_tips") or [])[:2]:
        tips.append(tip)
    tt = trail_tune or {}
    for epic, row in list((tt.get("by_epic") or {}).items())[:2]:
        cap = row.get("median_capture_ratio")
        if cap is not None:
            tips.append(
                f"Trail tune {row.get('market')}: "
                f"trigger={row.get('trail_trigger_atr_multiple')}×ATR "
                f"distance={row.get('trail_distance_atr_multiple')}×ATR "
                f"(capture {float(cap):.0%})"
            )
    if not tips:
        tips.append("Continue shadow tail + daily v26_learning_pack.")
    return tips


def write_learning_snapshot(
    *,
    days: list[str] | None = None,
    max_days: int | None = None,
) -> Path:
    payload = build_learning_snapshot(days=days, max_days=max_days)
    out_dir = _project_root() / "data_lake" / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "v26_learning_snapshot.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
