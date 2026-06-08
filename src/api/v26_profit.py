"""Read-only v26 PROFIT tab payload from data lake snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.paths import project_root


def _state_dir() -> Path:
    return project_root() / "data_lake" / "state"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _load_milestones() -> dict[str, Any]:
    path = project_root() / "config" / "config_v26.json"
    raw = _read_json(path) or {}
    block = raw.get("milestones") or {}
    return {
        "current": str(block.get("current") or "M0"),
        "prove_days": int(block.get("prove_days") or 14),
        "targets": block.get("targets") or {},
    }


def _milestone_progress(
    portfolio: dict[str, Any], milestones: dict[str, Any]
) -> dict[str, Any]:
    current = str(milestones.get("current") or "M0")
    targets: dict[str, Any] = milestones.get("targets") or {}
    target_gbp = float(targets.get(current) or 0)
    rolling_days = max(1, int(portfolio.get("rolling_days") or 14))
    total = float(portfolio.get("total_pnl_gbp") or 0)
    daily_avg = total / rolling_days
    progress_pct = (
        min(100.0, (daily_avg / target_gbp) * 100.0) if target_gbp > 0 else 0.0
    )
    return {
        "current": current,
        "target_daily_gbp": target_gbp,
        "rolling_daily_avg_gbp": round(daily_avg, 2),
        "progress_pct": round(progress_pct, 1),
    }


def _load_daily_progress() -> dict[str, Any]:
    raw = _read_json(_state_dir() / "v26_daily_progress.json")
    return raw if raw else {}


def _load_learning_snapshot() -> dict[str, Any]:
    raw = _read_json(_state_dir() / "v26_learning_snapshot.json")
    return raw if raw else {}


def _load_ohlc_replay() -> dict[str, Any]:
    raw = _read_json(_state_dir() / "v26_ohlc_replay.json")
    return raw if raw else {}


def _load_trade_learning(learning: dict[str, Any]) -> dict[str, Any]:
    raw = _read_json(_state_dir() / "v26_trade_learning.json")
    if raw:
        return raw
    return learning.get("trade_learning") or {}


def build_profit_payload() -> dict[str, Any]:
    state_dir = _state_dir()
    expectancy = _read_json(state_dir / "expectancy_snapshot.json") or {}
    shadow_pnl = _read_json(state_dir / "shadow_strategy_pnl.json") or {}
    progress = _load_daily_progress()
    learning = _load_learning_snapshot()
    ohlc = _load_ohlc_replay() or learning.get("ohlc_replay") or {}
    trade_learning = _load_trade_learning(learning)
    milestones = _load_milestones()

    portfolio = expectancy.get("portfolio") or {}
    if expectancy.get("rolling_days"):
        portfolio = {**portfolio, "rolling_days": expectancy["rolling_days"]}

    live_envelope: dict[str, Any] = {}
    try:
        from system.portfolio_envelope import snapshot as live_envelope_snapshot

        live_envelope = live_envelope_snapshot()
    except Exception:
        live_envelope = {}

    return {
        "ok": bool(expectancy),
        "generated_at": expectancy.get("generated_at")
        or shadow_pnl.get("generated_at"),
        "rolling_days": expectancy.get("rolling_days") or 14,
        "portfolio": portfolio,
        "setups": expectancy.get("setups") or [],
        "shadow_strategies": shadow_pnl.get("by_strategy") or {},
        "shadow_attributed_fills": shadow_pnl.get("attributed_fills") or 0,
        "shadow_total_fills": shadow_pnl.get("total_fills") or 0,
        "milestones": _milestone_progress(portfolio, milestones),
        "milestone_targets": milestones.get("targets") or {},
        "daily_progress": progress,
        "gate_blockers": progress.get("gate_blockers") or {},
        "l1_replay": progress.get("l1_replay") or {},
        "near_miss": (progress.get("shadow_expectancy") or {}),
        "learning": learning,
        "l1_certification": learning.get("l1_certification") or {},
        "bar_analysis": learning.get("bar_analysis_latest") or {},
        "shadow_summary": learning.get("shadow_summary") or {},
        "learning_focus": learning.get("learning_focus") or [],
        "ohlc_replay": ohlc,
        "bar_lab_historical": ohlc.get("bar_lab") or {},
        "walk_forward": ohlc.get("walk_forward") or {},
        "portfolio_envelope": {
            **(learning.get("portfolio_envelope") or {}),
            **({"live": live_envelope} if live_envelope else {}),
        },
        "s1_phase2": (learning.get("shadow_summary") or {})
        .get("by_strategy", {})
        .get("S1_rules_v25", {})
        .get("s1_phase2", 0),
        "trade_learning": trade_learning,
        "ml_readiness": trade_learning.get("ml_readiness") or {},
    }
