"""
Append-only JSONL event bus: v25 agent → data_lake/events/ for v26 learning.

Schema: shared/contracts/event_schema.json (contract_version 1.0)
Never raises into the trading loop — failures are swallowed and logged once.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import feeder_events_dir

CONTRACT_VERSION = "1.0"

_lock = threading.Lock()
_enabled_override: bool | None = None
_emit_errors_logged = 0


def is_enabled() -> bool:
    if _enabled_override is not None:
        return _enabled_override
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return False
    if os.environ.get("IG_AGENT_FEEDER", "1").strip() in ("0", "false", "no"):
        return False
    return True


def set_enabled_for_tests(enabled: bool | None) -> None:
    global _enabled_override
    _enabled_override = enabled


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _daily_path(root: Path) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return root / f"{day}.jsonl"


def emit(
    event_type: str,
    *,
    epic: str,
    market: str = "",
    session: str = "",
    payload: dict[str, Any] | None = None,
    ts: str | None = None,
) -> None:
    """Append one feeder event. No-op when disabled."""
    if not is_enabled():
        return
    row = {
        "contract_version": CONTRACT_VERSION,
        "event_type": event_type,
        "ts": ts or _utc_now(),
        "epic": str(epic or ""),
        "market": str(market or ""),
        "session": str(session or ""),
        "payload": payload or {},
    }
    try:
        root = feeder_events_dir()
        root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, default=str, separators=(",", ":")) + "\n"
        with _lock:
            with open(_daily_path(root), "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        global _emit_errors_logged
        if _emit_errors_logged < 3:
            _emit_errors_logged += 1
            log_engine(f"feeder emit failed ({event_type}): {type(e).__name__}: {e}")


def emit_bar_close(
    *,
    epic: str,
    market: str,
    session: str,
    bar_time: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 0.0,
) -> None:
    emit(
        "bar_close",
        epic=epic,
        market=market,
        session=session,
        payload={
            "bar_time": bar_time,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
    )


def emit_signal_eval(
    *,
    epic: str,
    market: str,
    session: str,
    direction: str,
    raw_score: float,
    adjusted_score: float,
    setup_key: str,
    would_fire: bool,
    reason: str = "",
    snapshot: dict[str, Any] | None = None,
    gates_passed: list[str] | None = None,
    ml_probability: float | None = None,
    threshold_pass: dict[str, bool] | None = None,
    risk_band: str = "",
    pilot_epic: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "direction": direction,
        "raw_score": raw_score,
        "adjusted_score": adjusted_score,
        "setup_key": setup_key,
        "would_fire": would_fire,
        "reason": reason,
    }
    if gates_passed is not None:
        payload["gates_passed"] = gates_passed
    if ml_probability is not None:
        payload["ml_probability"] = ml_probability
    if threshold_pass:
        payload["threshold_pass"] = threshold_pass
    if risk_band:
        payload["risk_band"] = risk_band
    if pilot_epic:
        payload["pilot"] = True
    if snapshot:
        payload["snapshot"] = snapshot
    emit(
        "signal_eval",
        epic=epic,
        market=market,
        session=session,
        payload=payload,
    )


def emit_regime_snapshot(
    *,
    epic: str,
    market: str,
    session: str,
    fitness: float | None = None,
    vol_regime: str = "",
    points_state: str = "",
    spread: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "fitness": fitness,
        "vol_regime": vol_regime,
        "points_state": points_state,
        "spread": spread,
    }
    if extra:
        payload.update(extra)
    emit(
        "regime_snapshot",
        epic=epic,
        market=market,
        session=session,
        payload=payload,
    )


def emit_gate_result(
    *,
    epic: str,
    market: str,
    session: str,
    gate_name: str,
    passed: bool,
    detail: str = "",
    value: Any = None,
) -> None:
    payload: dict[str, Any] = {
        "gate_name": gate_name,
        "passed": passed,
        "detail": detail,
    }
    if value is not None:
        if isinstance(value, dict):
            payload["value"] = value
        else:
            payload["value_summary"] = str(value)[:500]
    emit(
        "gate_result",
        epic=epic,
        market=market,
        session=session,
        payload=payload,
    )


def emit_order_intent(
    *,
    epic: str,
    market: str,
    session: str,
    direction: str,
    size: float,
    confidence: float,
    setup_key: str,
    risk_gbp: float,
    stop_points: float,
) -> None:
    emit(
        "order_intent",
        epic=epic,
        market=market,
        session=session,
        payload={
            "direction": direction,
            "size": size,
            "confidence": confidence,
            "setup_key": setup_key,
            "risk_gbp": risk_gbp,
            "stop_points": stop_points,
        },
    )


def emit_fill_open(
    *,
    epic: str,
    market: str,
    trade_id: int,
    deal_id: str,
    direction: str,
    entry: float,
    size: float,
    stop: float,
    target: float,
    confidence: float,
    setup_key: str,
    risk_gbp: float | None = None,
) -> None:
    emit(
        "fill_open",
        epic=epic,
        market=market,
        payload={
            "trade_id": trade_id,
            "deal_id": deal_id,
            "direction": direction,
            "entry": entry,
            "size": size,
            "stop": stop,
            "target": target,
            "confidence": confidence,
            "setup_key": setup_key,
            "risk_gbp": risk_gbp,
        },
    )


def emit_fill_close(
    *,
    epic: str,
    market: str,
    trade_id: int | None,
    deal_id: str,
    pnl_gbp: float,
    pnl_points: float,
    result: str,
    exit_reason: str,
    setup_key: str,
    confidence: float,
    risk_gbp: float | None = None,
) -> None:
    emit(
        "fill_close",
        epic=epic,
        market=market,
        payload={
            "trade_id": trade_id,
            "deal_id": deal_id,
            "pnl_gbp": pnl_gbp,
            "pnl_points": pnl_points,
            "result": result,
            "exit_reason": exit_reason,
            "setup_key": setup_key,
            "confidence": confidence,
            "risk_gbp": risk_gbp,
        },
    )
