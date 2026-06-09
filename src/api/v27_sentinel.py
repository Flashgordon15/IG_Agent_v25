"""v27 Autonomous Sentinel — dashboard API payloads."""

from __future__ import annotations

import json
from typing import Any

from ai.operational.auto_repair import AutoRepairEngine
from ai.operational.system_monitor import get_system_monitor
from ai.paths import operational_safety_freeze_path, sentinel_diagnostics_path
from ai.strategy.backtest_simulator import BacktestSimulator, load_strategy_proposals
from ai.strategy.performance_reviewer import (
    build_friction_matrix,
    read_quotes_from_dashboard_snapshot,
)


def build_sentinel_diagnostics(limit: int = 80) -> dict[str, Any]:
    monitor = get_system_monitor()
    snap = monitor.snapshot()
    lines: list[dict[str, Any]] = monitor.recent_diagnostics(limit=limit)
    if not lines and sentinel_diagnostics_path().exists():
        try:
            raw = (
                sentinel_diagnostics_path()
                .read_text(encoding="utf-8")
                .strip()
                .splitlines()
            )
            for line in raw[-limit:]:
                lines.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass

    freeze: dict[str, Any] | None = None
    fpath = operational_safety_freeze_path()
    if fpath.exists():
        try:
            freeze = json.loads(fpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            freeze = None

    quotes = read_quotes_from_dashboard_snapshot()
    epics = list(quotes.keys())[:12]
    friction = (
        build_friction_matrix(epics, quotes=quotes)
        if epics
        else {"cells": [], "eligible": True}
    )

    profiler_latency: dict[str, Any] = {}
    try:
        from ai.operational.profiler import get_operational_profiler

        profiler_latency = get_operational_profiler().rolling_percentiles()
    except Exception:
        pass

    return {
        "monitor": snap,
        "safety_freeze": freeze,
        "friction_matrix": friction,
        "profiler_latency": profiler_latency,
        "lines": lines,
        "proposals": load_strategy_proposals(),
    }


def approve_strategy_proposal(proposal_id: str) -> dict[str, Any]:
    sim = BacktestSimulator()
    approved = sim.approve_proposal(proposal_id)
    if approved is None:
        return {"ok": False, "error": "proposal_not_found", "proposal_id": proposal_id}

    engine = AutoRepairEngine()
    results = engine.check_approved_proposals()
    result = results[0] if results else {"ok": False, "detail": "validation_not_run"}
    return {
        "ok": bool(result.get("ok")),
        "proposal": approved,
        "validation_result": result,
    }
