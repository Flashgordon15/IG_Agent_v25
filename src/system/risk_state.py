"""
Unified risk_state API — volatility engine + existing risk guards.
"""

from __future__ import annotations

import time
from typing import Any


def get_risk_state_snapshot() -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": True,
        "endpoint": "risk_state",
        "ts": time.time(),
    }
    try:
        from system.volatility_risk_engine import (
            circuit_breaker_blocks_entry,
            get_volatility_risk_snapshot,
        )

        vol = get_volatility_risk_snapshot()
        blocked, block_reason = circuit_breaker_blocks_entry()
        body["volatility_risk"] = vol
        body["circuit_breaker"] = {
            "level": vol.get("circuit_breaker_level", 0),
            "blocked": blocked,
            "reason": block_reason,
            "halt_until_ts": vol.get("halt_until_ts", 0),
            "drawdown_pct": vol.get("intraday_drawdown_pct", 0),
        }
        body["ok"] = bool(vol.get("ok", True)) and not blocked
    except Exception as exc:
        body["volatility_risk"] = {"ok": False, "error": type(exc).__name__}
        body["ok"] = False

    try:
        from trading.manual_intervention import risk_status

        body["shield"] = risk_status()
    except Exception:
        body["shield"] = {}

    try:
        from runtime.strategy_kill_switch import kill_switch_snapshot

        body["kill_switch"] = kill_switch_snapshot()
        if body["kill_switch"].get("active"):
            body["ok"] = False
    except Exception:
        body["kill_switch"] = {"active": False}

    try:
        from analytics.tuning_params import get_tuning_params

        tp = get_tuning_params().get("params") or {}
        body["tuning"] = {
            "risk_per_trade_gbp": tp.get("risk_per_trade_gbp"),
            "trailing_sensitivity": tp.get("trailing_sensitivity"),
            "dynamic_limit_scale": tp.get("dynamic_limit_scale"),
        }
    except Exception:
        body["tuning"] = {}

    return body
