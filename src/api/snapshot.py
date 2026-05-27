"""
Dashboard tick snapshot schema — Section 4.5 Step 8.

WebSocket /ws and GET /state share this payload. Trading loop publishes via
snapshot_store.publish_tick() (Step 9).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

GATE_NAMES: tuple[str, ...] = (
    "session_open",
    "cold_start_gap",
    "environment_fitness",
    "points_state",
    "risk_validation",
    "signal_confidence",
    "execution",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_gates() -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    for name in GATE_NAMES:
        gates.append(
            {
                "name": name,
                "pass": False,
                "value": None,
                "detail": "Trading engine not connected",
            }
        )
    return gates


def build_default_tick() -> dict[str, Any]:
    """Safe offline snapshot when no trading loop has published yet."""
    gates = _default_gates()
    passing = sum(1 for g in gates if g.get("pass"))
    return {
        "type": "tick",
        "ts": _iso_now(),
        "market_state": "OFFLINE",
        "bid": None,
        "offer": None,
        "spread": None,
        "tick_age_s": None,
        "stream_status": "DISCONNECTED",
        "rest_calls_min": 0,
        "errors": {"count": 0, "type": None},
        "health": {
            "badge": "BLOCKED",
            "gates": gates,
            "summary": f"{passing} of {len(gates)} gates passing — awaiting engine",
        },
        "signal": {
            "direction": "WAIT",
            "confidence": 0.0,
            "fitness": 0,
            "atr": 0.0,
            "setup": "",
        },
        "points": {
            "state": "CAUTION",
            "cumulative": 0.0,
            "session": 0.0,
            "last_trade": 0.0,
            "size_multiplier": 1.0,
        },
        "positions": [],
        "daily_pnl_gbp": 0.0,
        "balance_gbp": None,
        "win_rate_20": None,
    }


def normalize_tick(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge partial publisher updates onto defaults; always include type tick."""
    base = build_default_tick()
    if not isinstance(payload, dict):
        return base
    for key, val in payload.items():
        if key == "health" and isinstance(val, dict):
            merged = dict(base["health"])
            merged.update(val)
            if isinstance(val.get("gates"), list):
                merged["gates"] = val["gates"]
            base["health"] = merged
        elif key in ("signal", "points", "errors") and isinstance(val, dict):
            merged = dict(base[key])
            merged.update(val)
            base[key] = merged
        elif key == "positions" and isinstance(val, list):
            base["positions"] = val
        else:
            base[key] = val
    base["type"] = "tick"
    if not base.get("ts"):
        base["ts"] = _iso_now()
    return base
