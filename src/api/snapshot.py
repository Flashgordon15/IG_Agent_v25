"""
Dashboard tick snapshot schema — Section 4.5 Step 8.

WebSocket /ws and GET /state share this payload. Trading loop publishes via
snapshot_store.publish_tick() (Step 9).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from system.paths import logs_dir

_DEBUG_SIGNAL_KEYS_LOGGED = False

GATE_NAMES: tuple[str, ...] = (
    "session_open",
    "cold_start_gap",
    "environment_fitness",
    "points_state",
    "correlation_ok",
    "risk_validation",
    "expectancy_ok",
    "calendar_ok",
    "signal_confidence",
    "ml_veto",
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
    from trading.gate_readiness import compute_trade_readiness, format_health_badge_text

    gates = _default_gates()
    passing = sum(1 for g in gates if g.get("pass"))
    badge = "BLOCKED"
    readiness = compute_trade_readiness(gates)
    badge_text = format_health_badge_text(badge, readiness)
    return {
        "type": "tick",
        "ts": _iso_now(),
        "market_state": "OFFLINE",  # OPEN | CLOSED | OFFLINE | MAINTENANCE
        "bid": None,
        "offer": None,
        "spread": None,
        "tick_age_s": None,
        "stream_status": "DISCONNECTED",
        "rest_calls_min": 0,
        "errors": {"count": 0, "type": None},
        "health": {
            "badge": badge,
            "badge_text": badge_text,
            "readiness": readiness,
            "gates": gates,
            "summary": f"{passing} of {len(gates)} gates passing — awaiting engine",
        },
        "signal": {
            "direction": "WAIT",
            "confidence": 0.0,
            "threshold": 70,
            "config_signal_threshold": 70,
            "points_state": "CAUTION",
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


def _signal_confidence_gate_value(tick: dict[str, Any]) -> dict[str, Any] | None:
    for g in (tick.get("health") or {}).get("gates") or []:
        if isinstance(g, dict) and g.get("name") == "signal_confidence":
            val = g.get("value")
            if isinstance(val, dict):
                return val
    return None


def _int_pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _fallback_thresholds_for_state(state: str | None) -> dict[str, int]:
    """Display fallbacks when gate/signal omit threshold fields (hub quote-only ticks)."""
    from trading.points_engine import CONF_HIGH, CONF_MARGINAL_MIN, CONF_STANDARD_MIN

    st = (state or "CAUTION").upper()
    if st == "WARNING":
        floor = int(CONF_HIGH)
        min_size = int(CONF_HIGH)
    elif st == "CAUTION":
        floor = int(CONF_MARGINAL_MIN)
        min_size = 88
    elif st == "HEALTHY":
        floor = int(CONF_MARGINAL_MIN)
        min_size = int(CONF_STANDARD_MIN)
    else:
        floor = int(CONF_MARGINAL_MIN)
        min_size = int(CONF_MARGINAL_MIN)
    return {"points_confidence_floor": floor, "min_size_threshold": min_size}


def enrich_signal_thresholds(tick: dict[str, Any]) -> None:
    """
    Ensure top-level signal carries config / effective / min-size thresholds.

    The Live dashboard reads signal.config_signal_threshold and
    signal.min_size_threshold on every WebSocket tick. Hub quote merges can
    republish a cached tick that predates those fields; copy from the
    signal_confidence gate when present, otherwise derive from points state.
    """
    signal = tick.get("signal")
    if not isinstance(signal, dict):
        signal = {}
        tick["signal"] = signal

    gate = _signal_confidence_gate_value(tick)
    points = tick.get("points") if isinstance(tick.get("points"), dict) else {}
    state = (
        signal.get("points_state")
        or (gate.get("points_state") if gate else None)
        or points.get("state")
    )

    if gate:
        for key in (
            "threshold",
            "config_signal_threshold",
            "points_confidence_floor",
            "min_size_threshold",
            "risk_band",
            "threshold_pass",
            "probe_risk_gbp_target",
            "sizing_risk_gbp",
        ):
            if key == "threshold_pass":
                if isinstance(gate.get(key), dict):
                    signal[key] = dict(gate[key])
                continue
            if key == "risk_band":
                if gate.get(key):
                    signal[key] = str(gate[key])
                continue
            pct = _int_pct(gate.get(key))
            if pct is not None:
                signal[key] = pct
        if gate.get("points_state"):
            signal["points_state"] = str(gate["points_state"])

    for g in (tick.get("health") or {}).get("gates") or []:
        if not isinstance(g, dict) or g.get("name") != "risk_validation":
            continue
        val = g.get("value")
        if not isinstance(val, dict):
            continue
        if not signal.get("risk_band") and val.get("risk_band"):
            signal["risk_band"] = str(val["risk_band"])
        if signal.get("sizing_risk_gbp") is None and val.get("risk_gbp") is not None:
            try:
                signal["sizing_risk_gbp"] = int(round(float(val["risk_gbp"])))
            except (TypeError, ValueError):
                pass
        break

    if signal.get("config_signal_threshold") is None:
        try:
            from system.config_loader import load_config

            signal["config_signal_threshold"] = _int_pct(load_config().signal_threshold)
        except Exception:
            signal["config_signal_threshold"] = 70

    fallbacks = _fallback_thresholds_for_state(
        str(state) if state is not None else None
    )
    if signal.get("points_confidence_floor") is None:
        signal["points_confidence_floor"] = fallbacks["points_confidence_floor"]
    if _int_pct(gate.get("min_size_threshold") if gate else None) is None:
        signal["min_size_threshold"] = fallbacks["min_size_threshold"]

    if signal.get("threshold") is None:
        cfg = int(signal.get("config_signal_threshold") or 70)
        floor = int(signal.get("points_confidence_floor") or 80)
        signal["threshold"] = max(cfg, floor)

    if signal.get("points_state") is None and state is not None:
        signal["points_state"] = str(state)


def _log_signal_keys_once(tick: dict[str, Any]) -> None:
    """One-time debug line to launcher.log — confirms normalize_tick signal shape."""
    global _DEBUG_SIGNAL_KEYS_LOGGED
    if (
        _DEBUG_SIGNAL_KEYS_LOGGED
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
    ):
        return
    _DEBUG_SIGNAL_KEYS_LOGGED = True
    signal = tick.get("signal")
    keys = sorted(signal.keys()) if isinstance(signal, dict) else []
    sample = {}
    if isinstance(signal, dict):
        for key in (
            "config_signal_threshold",
            "min_size_threshold",
            "threshold",
            "confidence",
            "points_state",
        ):
            if key in signal:
                sample[key] = signal[key]
    line = (
        f"normalize_tick signal keys={keys!r} sample={sample!r} "
        f"points.state={(tick.get('points') or {}).get('state')!r}"
    )
    log_path = logs_dir() / "launcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{stamp} | {line}\n")


def normalize_tick(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge partial publisher updates onto defaults; always include type tick."""
    base = build_default_tick()
    if not isinstance(payload, dict):
        enrich_signal_thresholds(base)
        _log_signal_keys_once(base)
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
    enrich_signal_thresholds(base)
    _log_signal_keys_once(base)
    return base
