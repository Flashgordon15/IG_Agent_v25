"""
Shadow Brain Loop — observe-only gate funnel + live tolerance tuning.

Runs on IG_AGENT_MODE=SHADOW (:9199). No broker accounting, no simulated fills.
Maps near-miss gate blocks against the historical success matrix and publishes
floor adjustments for the Live Vanguard track.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.models import Quote
from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, project_root

_MARGIN_MIN_PCT = 1.0
_MARGIN_MAX_PCT = 5.0

_LOCK = threading.Lock()
_TICK_COUNT = 0
_YAHOO_TICK_COUNT = 0
_STALE_TICK_COUNT = 0
_BRAIN_EVENTS: list[dict[str, Any]] = []
_SUCCESS_MATRIX: dict[str, Any] | None = None


def shadow_brain_active() -> bool:
    try:
        from system.agent_execution_mode import shadow_execution_active

        return bool(shadow_execution_active())
    except Exception:
        import os

        return os.environ.get("IG_AGENT_MODE", "").strip().upper() == "SHADOW"


def _matrix_report_path() -> Path:
    return data_dir() / "matrix_backtuner_report.json"


def _load_success_matrix() -> dict[str, Any]:
    global _SUCCESS_MATRIX
    if _SUCCESS_MATRIX is not None:
        return _SUCCESS_MATRIX
    path = _matrix_report_path()
    if not path.is_file():
        alt = project_root() / "src" / "data" / "matrix_backtuner_report.json"
        path = alt if alt.is_file() else path
    matrix: dict[str, Any] = {"gates": {}, "best_candidate": None}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                matrix["best_candidate"] = raw.get("best_candidate")
                matrix["baseline_floors"] = raw.get("baseline_floors")
        except (OSError, json.JSONDecodeError):
            pass
    _SUCCESS_MATRIX = matrix
    return matrix


def _production_floors() -> dict[str, float]:
    try:
        from intelligence.matrix_backtuner import resolve_floor_bases, _load_merged_config

        bases = resolve_floor_bases(_load_merged_config())
        return {
            "signal_threshold_floor": float(bases.signal_confidence_pct),
            "fitness_min_floor": float(bases.environment_fitness_pct),
            "ml_veto_min_probability": float(bases.ml_veto_probability),
        }
    except Exception:
        return {
            "signal_threshold_floor": 42.0,
            "fitness_min_floor": 55.0,
            "ml_veto_min_probability": 0.45,
        }


def _quote_health(quote: Quote) -> dict[str, Any]:
    global _YAHOO_TICK_COUNT, _STALE_TICK_COUNT

    source = str(getattr(quote, "source", "") or "hub").lower()
    age_s = 0.0
    try:
        ts = getattr(quote, "time", None)
        if ts is not None and hasattr(ts, "timestamp"):
            age_s = max(0.0, time.time() - float(ts.timestamp()))
    except Exception:
        age_s = 0.0
    is_yahoo = "yahoo" in source or source in ("mock_feed", "historical_replay", "replay")
    if is_yahoo:
        _YAHOO_TICK_COUNT += 1
    stale = age_s > 45.0
    if stale:
        _STALE_TICK_COUNT += 1
    status = "HEALTHY"
    if stale:
        status = "STALE"
    elif age_s > 10.0:
        status = "DEGRADED"
    return {
        "source": source or "hub",
        "quote_age_s": round(age_s, 2),
        "status": status,
        "is_yahoo_path": is_yahoo,
    }


def _gate_metric(gate_name: str, gate: Any) -> float | None:
    value = getattr(gate, "value", None)
    if gate_name == "signal_confidence" and isinstance(value, dict):
        try:
            return float(value.get("confidence") or 0)
        except (TypeError, ValueError):
            return None
    if gate_name == "environment_fitness":
        if isinstance(value, dict):
            try:
                return float(value.get("score") or 0)
            except (TypeError, ValueError):
                return None
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return None
    if gate_name == "ml_veto" and isinstance(value, dict):
        try:
            return float(value.get("ml_probability") or 0)
        except (TypeError, ValueError):
            return None
    return None


def _floor_for_gate(gate_name: str, floors: dict[str, float]) -> float | None:
    if gate_name == "signal_confidence":
        return floors.get("signal_threshold_floor")
    if gate_name == "environment_fitness":
        return floors.get("fitness_min_floor")
    if gate_name == "ml_veto":
        return floors.get("ml_veto_min_probability")
    return None


def _would_be_winner(gate_name: str, metric: float, matrix: dict[str, Any]) -> bool:
    best = matrix.get("best_candidate") or {}
    if gate_name == "signal_confidence":
        try:
            return metric >= float(best.get("signal_confidence_floor_pct") or 0)
        except (TypeError, ValueError):
            return False
    if gate_name == "environment_fitness":
        try:
            return metric >= float(best.get("environment_fitness_floor_pct") or 0)
        except (TypeError, ValueError):
            return False
    if gate_name == "ml_veto":
        try:
            return metric >= float(best.get("ml_veto_floor_probability") or 0)
        except (TypeError, ValueError):
            return False
    return False


@dataclass
class NearMissResult:
    gate_name: str
    margin_pct: float
    metric: float
    floor: float
    would_win: bool
    adjustment: dict[str, float] = field(default_factory=dict)


def _compute_near_miss(
    gate_name: str,
    gate: Any,
    *,
    floors: dict[str, float],
    matrix: dict[str, Any],
) -> NearMissResult | None:
    if bool(getattr(gate, "passed", True)):
        return None
    metric = _gate_metric(gate_name, gate)
    floor = _floor_for_gate(gate_name, floors)
    if metric is None or floor is None or floor <= 0:
        return None
    if gate_name == "ml_veto":
        gap = floor - metric
        margin = (gap / max(floor, 1e-9)) * 100.0
    else:
        gap = floor - metric
        margin = (gap / max(floor, 1e-9)) * 100.0
    if margin < _MARGIN_MIN_PCT or margin > _MARGIN_MAX_PCT:
        return None
    would_win = _would_be_winner(gate_name, metric, matrix)
    if not would_win:
        return None
    relax = gap * 0.5
    adjustment: dict[str, float] = {}
    if gate_name == "signal_confidence":
        adjustment["signal_threshold_floor"] = round(max(10.0, floor - relax), 3)
    elif gate_name == "environment_fitness":
        adjustment["fitness_min_floor"] = round(max(10.0, floor - relax), 3)
    elif gate_name == "ml_veto":
        adjustment["ml_veto_min_probability"] = round(max(0.05, floor - relax), 4)
    return NearMissResult(
        gate_name=gate_name,
        margin_pct=round(margin, 3),
        metric=metric,
        floor=floor,
        would_win=True,
        adjustment=adjustment,
    )


def process_shadow_brain_tick(
    *,
    epic: str,
    market: str,
    quote: Quote,
    gates: list[Any],
    gate_snapshot: dict[str, bool],
    signal: Any | None = None,
    fitness: float = 0.0,
) -> dict[str, Any]:
    """Observe-only brain tick — funnel telemetry + optional live tolerance publish."""
    global _TICK_COUNT

    with _LOCK:
        _TICK_COUNT += 1

    try:
        from intelligence.matrix_prebaker import record_ingestion_tick

        record_ingestion_tick()
    except Exception:
        pass

    health = _quote_health(quote)
    floors = _production_floors()
    matrix = _load_success_matrix()
    natural_pass = all(bool(g.passed) for g in gates)
    first_fail = next((g for g in gates if not g.passed), None)
    fail_name = str(getattr(first_fail, "name", "") or "")

    direction = "WAIT"
    confidence = 0.0
    rsi = 0.0
    atr = 0.0
    if signal is not None:
        direction = str(getattr(signal, "signal", "WAIT") or "WAIT")
        try:
            confidence = float(getattr(signal, "adjusted_confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        snap = getattr(signal, "snapshot", None) or {}
        if isinstance(snap, dict):
            try:
                rsi = float(snap.get("rsi") or snap.get("hud_rsi") or 0)
            except (TypeError, ValueError):
                rsi = 0.0
            try:
                atr = float(snap.get("atr") or 0)
            except (TypeError, ValueError):
                atr = 0.0

    would_take = direction in ("BUY", "SELL") and natural_pass
    near_miss: NearMissResult | None = None
    if first_fail is not None and fail_name in (
        "signal_confidence",
        "environment_fitness",
        "ml_veto",
    ):
        near_miss = _compute_near_miss(fail_name, first_fail, floors=floors, matrix=matrix)

    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epic": epic,
        "market": market,
        "data_health": health,
        "would_take": would_take,
        "direction": direction,
        "confidence": round(confidence, 2),
        "fitness": round(float(fitness), 2),
        "natural_pass": natural_pass,
        "first_fail_gate": fail_name or None,
        "gate_snapshot": dict(gate_snapshot),
        "near_miss": None,
    }

    if near_miss is not None:
        event["near_miss"] = {
            "gate": near_miss.gate_name,
            "margin_pct": near_miss.margin_pct,
            "metric": near_miss.metric,
            "floor": near_miss.floor,
            "adjustment": near_miss.adjustment,
        }
        try:
            from trading.shadow_executor import dispatch_live_tolerance_to_vanguard

            merged = dict(floors)
            merged.update(near_miss.adjustment)
            dispatch_result = dispatch_live_tolerance_to_vanguard(
                {"live_floors": merged, **near_miss.adjustment},
                epic=epic,
                market=market,
                direction=direction,
                rsi=rsi,
                atr=atr,
                near_miss_gate=near_miss.gate_name,
                margin_pct=near_miss.margin_pct,
            )
            event["live_dispatch"] = dispatch_result
            if dispatch_result.get("dispatched") or dispatch_result.get("manifest_fallback"):
                log_engine(
                    f"SHADOW_BRAIN near-miss tune epic={epic} gate={near_miss.gate_name} "
                    f"margin={near_miss.margin_pct:.2f}% adj={near_miss.adjustment} "
                    f"archive={dispatch_result.get('archive_outcome')}"
                )
            else:
                log_engine(
                    f"SHADOW_BRAIN dispatch blocked epic={epic} "
                    f"reason={dispatch_result.get('blocked_reason')} "
                    f"archive={dispatch_result.get('archive_outcome')}"
                )
        except Exception as exc:
            log_guarded_exception("shadow_brain_loop", exc)

    with _LOCK:
        _BRAIN_EVENTS.append(event)
        if len(_BRAIN_EVENTS) > 200:
            del _BRAIN_EVENTS[:-200]

    return event


def brain_dashboard_payload() -> dict[str, Any]:
    with _LOCK:
        tick_count = _TICK_COUNT
        yahoo_ticks = _YAHOO_TICK_COUNT
        stale_ticks = _STALE_TICK_COUNT
        events = list(_BRAIN_EVENTS[-40:])
    health_status = "HEALTHY"
    if tick_count > 0 and stale_ticks / max(1, tick_count) > 0.25:
        health_status = "STALE"
    elif tick_count > 0 and yahoo_ticks / max(1, tick_count) < 0.5:
        health_status = "DEGRADED"

    funnel: dict[str, Any] = {}
    try:
        from trading.gate_funnel_counter import read_funnel_snapshot

        funnel = read_funnel_snapshot()
    except Exception:
        pass

    tolerance: dict[str, Any] = {}
    try:
        from system.identity.live_tolerance_bridge import brain_telemetry_snapshot

        tolerance = brain_telemetry_snapshot()
    except Exception:
        pass

    floors = _production_floors()
    manifest = tolerance.get("manifest") or {}
    live_floors = manifest.get("live_floors") or manifest.get("adjustments") or floors

    sequential: list[dict[str, Any]] = []
    block_counts = funnel.get("first_block_counts") or {}
    for gate_name, details in block_counts.items():
        if not isinstance(details, dict):
            continue
        total = sum(int(v) for v in details.values())
        top_detail = max(details.items(), key=lambda kv: kv[1])[0] if details else ""
        sequential.append(
            {
                "gate": gate_name,
                "dropouts": total,
                "top_reason": top_detail,
            }
        )
    sequential.sort(key=lambda row: -int(row.get("dropouts") or 0))

    return {
        "mode": "SHADOW_BRAIN",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_health": {
            "status": health_status,
            "total_incoming_ticks": int(funnel.get("total_ticks") or tick_count),
            "yahoo_ticks": yahoo_ticks,
            "stale_ticks": stale_ticks,
            "all_passed_ticks": int(funnel.get("all_passed_ticks") or 0),
        },
        "gate_funnel": {
            "sequential_dropouts": sequential,
            "raw": funnel,
        },
        "live_tolerance_output": {
            "baseline_floors": floors,
            "active_floors": live_floors,
            "last_publish": manifest,
        },
        "recent_events": events,
        "success_matrix": _load_success_matrix().get("best_candidate"),
    }


def reset_shadow_brain_for_tests() -> None:
    global _TICK_COUNT, _YAHOO_TICK_COUNT, _STALE_TICK_COUNT, _BRAIN_EVENTS, _SUCCESS_MATRIX
    with _LOCK:
        _TICK_COUNT = 0
        _YAHOO_TICK_COUNT = 0
        _STALE_TICK_COUNT = 0
        _BRAIN_EVENTS = []
        _SUCCESS_MATRIX = None
