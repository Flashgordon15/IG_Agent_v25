"""
Regime router v0 — pick which shadow strategies evaluate each feeder event.

Shadow-only: does not affect v25 live orders.
"""

from __future__ import annotations

from typing import Any

from strategies.base import StrategyPlugin

FX_EPICS = frozenset(
    {
        "CS.D.EURUSD.CFD.IP",
        "CS.D.GBPUSD.CFD.IP",
    }
)
FX_SESSIONS = frozenset(
    {
        "london_morning",
        "london_us_overlap",
        "us_morning",
        "us_afternoon",
    }
)
MOMENTUM_BLOCK_FX = True

_regime_by_epic: dict[str, dict[str, Any]] = {}


def update_regime_cache(event: dict[str, Any]) -> None:
    if str(event.get("event_type") or "") != "regime_snapshot":
        return
    epic = str(event.get("epic") or "")
    if not epic:
        return
    payload = event.get("payload") or {}
    _regime_by_epic[epic] = {
        "fitness": payload.get("fitness"),
        "vol_regime": str(payload.get("vol_regime") or ""),
        "points_state": str(payload.get("points_state") or ""),
        "spread": payload.get("spread"),
        "ts": str(event.get("ts") or ""),
    }


def regime_for_epic(epic: str) -> dict[str, Any]:
    return dict(_regime_by_epic.get(epic) or {})


def reset_regime_cache_for_tests() -> None:
    _regime_by_epic.clear()


def classify_regime(epic: str) -> str:
    """Coarse label for logging / shadow payload."""
    reg = regime_for_epic(epic)
    vol = str(reg.get("vol_regime") or "unknown").lower()
    if epic in FX_EPICS:
        return f"fx_{vol or 'normal'}"
    if vol in ("high", "low"):
        return vol
    return "normal"


def regime_blocks_strategy(epic: str, strategy_id: str) -> tuple[bool, str]:
    """Return (blocked, reason) from cached regime_snapshot."""
    reg = regime_for_epic(epic)
    if not reg:
        return False, ""

    points = str(reg.get("points_state") or "").upper()
    if points == "STOP":
        return True, "points STOP"

    vol = str(reg.get("vol_regime") or "").lower()
    fitness = reg.get("fitness")
    try:
        fit_f = float(fitness) if fitness is not None else None
    except (TypeError, ValueError):
        fit_f = None

    if strategy_id == "S2_momentum" and vol == "high":
        return True, "high vol — skip momentum"
    if strategy_id == "S3_session_fx" and vol == "high":
        return True, "high vol — skip mean reversion"
    if strategy_id == "S1_rules_v25" and fit_f is not None and fit_f < 25.0:
        return True, f"low fitness ({fit_f:.0f})"
    return False, ""


def route_strategies_for_event(
    event: dict[str, Any],
    strategies: list[StrategyPlugin],
) -> list[StrategyPlugin]:
    epic = str(event.get("epic") or "")
    session = str(event.get("session") or "")
    event_type = str(event.get("event_type") or "")
    routed: list[StrategyPlugin] = []

    for strat in strategies:
        sid = strat.strategy_id
        if sid == "S1_rules_v25":
            if event_type == "signal_eval":
                routed.append(strat)
        elif sid == "S2_momentum":
            if (
                event_type == "bar_close"
                and epic
                and (not MOMENTUM_BLOCK_FX or epic not in FX_EPICS)
            ):
                routed.append(strat)
        elif sid == "S3_session_fx":
            if (
                event_type == "bar_close"
                and epic in FX_EPICS
                and session in FX_SESSIONS
            ):
                routed.append(strat)
        else:
            routed.append(strat)

    filtered: list[StrategyPlugin] = []
    for strat in routed:
        blocked, _reason = regime_blocks_strategy(epic, strat.strategy_id)
        if not blocked:
            filtered.append(strat)
    return filtered
