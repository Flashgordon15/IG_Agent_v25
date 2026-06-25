"""
Decoupled 500ms fulfillment snapshot for Port 8080 UI.

Background thread aggregates feed / matrix / calibration / execution state.
FastAPI handlers read this cache only — zero work on Thread B hot path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}
_UI_STRESS_FLAG: dict[str, Any] = {}
_PERF_ROWS: deque[dict[str, Any]] = deque(maxlen=128)
_PHANTOM_ROW_SOURCES = frozenset(
    {
        "sim",
        "simulator",
        "test",
        "shadow_simulator",
        "shadow_force_fill",
        "bare_metal_phantom",
        "dry_run",
        "mock",
    }
)
_IG_POSITIONS_SYNCED_AT: float = 0.0
_IG_POSITIONS_SYNC_INTERVAL_SEC = 30.0
_REFRESH_THREAD: threading.Thread | None = None
_REFRESH_STOP = threading.Event()
_REFRESH_MS = 500
_GATE_DIAG: dict[str, dict[str, Any]] = {}
_LAST_GATE_DIAG: dict[str, Any] = {}
_FRONTIER_STATE: dict[str, dict[str, Any]] = {}
_LAST_FRONTIER: dict[str, Any] = {}
_PULSE_PATH = Path("/tmp/ig_fulfillment_pulse.json")
_PULSE_SERIAL = 0

# Data velocity watchdog — detects frozen RAM ingress (silent WebSockets).
_VELOCITY_LOCK = threading.Lock()
_VELOCITY_STATE: dict[str, Any] = {
    "last_ticks_cached": None,
    "last_live_ram_ticks": None,
    "last_race_wins": None,
    "last_change_mono": 0.0,
    "stalled_since_mono": None,
    "stall_active": False,
    "last_reset_mono": 0.0,
    "reset_count": 0,
}
VELOCITY_STALL_SEC = 5.0
VELOCITY_RESET_COOLDOWN_SEC = 30.0
CRITICAL_STALL_ALERT = (
    "⚠️ CRITICAL STALL: DATA STREAM DISCONNECTED - TRADING PAUSED"
)


def _quote_pulse_total(ring_tel: dict[str, Any]) -> int:
    total = 0
    for row in ring_tel.get("quote_ring") or []:
        try:
            total += int(row.get("win_seq") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _extract_velocity_metrics(
    *,
    matrix_stage: dict[str, Any],
    feed: dict[str, Any],
    ring_tel: dict[str, Any],
) -> dict[str, Any]:
    ticks_cached = int(matrix_stage.get("ticks_cached") or 0)
    race_wins = int(feed.get("race_wins_total") or 0)
    live_ram_ticks = _quote_pulse_total(ring_tel)
    if live_ram_ticks <= 0:
        live_ram_ticks = race_wins
    return {
        "ticks_cached": ticks_cached,
        "live_ram_ticks": live_ram_ticks,
        "race_wins_total": race_wins,
        "watchdog_metric": live_ram_ticks,
    }


def _trigger_feed_hard_reset(*, reason: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _VELOCITY_LOCK:
        last_reset = float(_VELOCITY_STATE.get("last_reset_mono") or 0.0)
        if now - last_reset < VELOCITY_RESET_COOLDOWN_SEC:
            return None
        _VELOCITY_STATE["last_reset_mono"] = now
        _VELOCITY_STATE["reset_count"] = int(_VELOCITY_STATE.get("reset_count") or 0) + 1
    try:
        from system.feeds.multi_feed_hub import hard_reset_multi_feed_hub

        return hard_reset_multi_feed_hub(reason=reason)
    except Exception:
        return None


def _update_data_velocity_watchdog(metrics: dict[str, Any]) -> dict[str, Any]:
    """Server-side 5s velocity watchdog — frozen live RAM ingress triggers hard reset."""
    now = time.monotonic()
    ticks = int(metrics.get("ticks_cached") or 0)
    live = int(metrics.get("live_ram_ticks") or 0)
    race = int(metrics.get("race_wins_total") or 0)

    with _VELOCITY_LOCK:
        prev_ticks = _VELOCITY_STATE.get("last_ticks_cached")
        prev_live = _VELOCITY_STATE.get("last_live_ram_ticks")
        prev_race = _VELOCITY_STATE.get("last_race_wins")

        live_changed = prev_live is None or live != prev_live or race != prev_race
        ticks_changed = prev_ticks is None or ticks != prev_ticks
        any_change = live_changed or ticks_changed

        if any_change:
            _VELOCITY_STATE["last_change_mono"] = now
            _VELOCITY_STATE["stalled_since_mono"] = None
            _VELOCITY_STATE["stall_active"] = False
            frozen_sec = 0.0
        else:
            frozen_sec = now - float(_VELOCITY_STATE.get("last_change_mono") or now)
            if frozen_sec >= VELOCITY_STALL_SEC:
                if _VELOCITY_STATE.get("stalled_since_mono") is None:
                    _VELOCITY_STATE["stalled_since_mono"] = now
                _VELOCITY_STATE["stall_active"] = True
            else:
                frozen_sec = 0.0

        _VELOCITY_STATE["last_ticks_cached"] = ticks
        _VELOCITY_STATE["last_live_ram_ticks"] = live
        _VELOCITY_STATE["last_race_wins"] = race

        stall_active = bool(_VELOCITY_STATE.get("stall_active"))
        reset_count = int(_VELOCITY_STATE.get("reset_count") or 0)

    feed_reset: dict[str, Any] | None = None
    if stall_active and frozen_sec >= VELOCITY_STALL_SEC:
        feed_reset = _trigger_feed_hard_reset(reason="data_velocity_stall")

    return {
        "ticks_cached": ticks,
        "live_ram_ticks": live,
        "race_wins_total": race,
        "watchdog_metric": live,
        "frozen_sec": round(float(frozen_sec), 2),
        "stall_active": stall_active,
        "stall_threshold_sec": VELOCITY_STALL_SEC,
        "trading_paused": stall_active,
        "critical_alert": CRITICAL_STALL_ALERT if stall_active else None,
        "feed_reset": feed_reset,
        "feed_reset_count": reset_count,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


def _apply_velocity_overrides(
    traffic: dict[str, Any],
    velocity: dict[str, Any],
    stages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Force crimson ingestion + trading paused when velocity watchdog trips."""
    if not velocity.get("stall_active"):
        return traffic, stages

    frozen = velocity.get("frozen_sec", 0)
    traffic = dict(traffic)
    traffic["ingestion"] = {
        "color": "crimson",
        "icon": "🔴",
        "label": "Ingestion Health",
        "detail": f"CRIMSON — data frozen {frozen:.0f}s (live RAM ticks stalled)",
        "stalled": True,
    }
    traffic["execution"] = {
        **(traffic.get("execution") or {}),
        "color": "crimson",
        "icon": "🔴",
        "label": "Master Execution Valve",
        "detail": "TRADING PAUSED — DATA STREAM DISCONNECTED",
        "stalled": True,
    }

    patched: list[dict[str, Any]] = []
    for stage in stages:
        row = dict(stage)
        if row.get("id") == 1:
            row["ok"] = False
            row["label"] = "🔴 DATA STREAM STALLED — feeds hard-reset triggered"
        patched.append(row)
    return traffic, patched


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "signal" and hasattr(item, "signal"):
                cleaned[key] = str(getattr(item, "signal", item))
            else:
                cleaned[key] = _sanitize_json(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record_gate_diagnostics(
    *,
    epic: str,
    gates: list[Any],
    wait_reason: str = "",
    all_passed: bool = False,
    tuning: dict[str, Any] | None = None,
) -> None:
    """Thread B — lightweight gate snapshot for 500ms UI (no disk I/O)."""
    compact: list[dict[str, Any]] = []
    for g in gates or []:
        name = str(getattr(g, "name", "") or "")
        passed = bool(getattr(g, "passed", False))
        detail = str(getattr(g, "detail", "") or "")
        value = getattr(g, "value", None)
        why = detail
        if not passed and not why:
            why = _format_gate_failure(name, value)
        compact.append(
            {
                "name": name,
                "passed": passed,
                "detail": detail,
                "why_failed": why if not passed else "",
                "value": _sanitize_json(value),
            }
        )
    from system.gating_reason import resolve_gating_reason

    gating_reason = resolve_gating_reason(
        epic=str(epic),
        wait_reason=str(wait_reason or ""),
        all_passed=bool(all_passed),
        gates=compact,
    )
    row = {
        "epic": epic,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "all_passed": bool(all_passed),
        "wait_reason": str(wait_reason or ""),
        "gating_reason": gating_reason,
        "gates": compact,
        "tuning": dict(tuning or {}),
    }
    with _CACHE_LOCK:
        _GATE_DIAG[str(epic)] = row
        _LAST_GATE_DIAG.clear()
        _LAST_GATE_DIAG.update(row)


def _format_gate_failure(name: str, value: Any) -> str:
    if not isinstance(value, dict):
        return f"{name} failed"
    if name == "signal_confidence":
        conf = value.get("confidence")
        floor = value.get("floor") or value.get("threshold")
        return f"signal_confidence failed: conf {conf} < {floor}% custom threshold"
    if name == "ml_veto":
        prob = value.get("ml_probability")
        floor = value.get("floor")
        return f"ml_veto failed: prob {prob} < {floor} custom threshold"
    if name == "environment_fitness":
        score = value.get("score")
        floor = value.get("floor")
        return f"environment_fitness failed: score {score} < floor {floor}"
    if name == "cold_start_gap":
        return f"cold_start_gap failed: {value.get('remaining_sec', '?')}s remaining"
    if name == "alpha_matrix_lookup":
        return f"alpha_matrix_lookup failed: {value.get('reason', 'cell miss')}"
    if name == "alpha_matrix_approved":
        return "alpha_matrix_approved failed: historical cell not winning"
    return f"{name} failed: {value}"


def get_gate_diagnostics_payload() -> dict[str, Any]:
    from system.gating_reason import resolve_gating_reason

    with _CACHE_LOCK:
        by_epic = dict(_GATE_DIAG)
        last = dict(_LAST_GATE_DIAG)
    for epic, row in by_epic.items():
        if not row.get("gating_reason"):
            row["gating_reason"] = resolve_gating_reason(
                epic=str(epic),
                wait_reason=str(row.get("wait_reason") or ""),
                all_passed=bool(row.get("all_passed")),
                gates=list(row.get("gates") or []),
            )
    if last and not last.get("gating_reason"):
        last["gating_reason"] = resolve_gating_reason(
            epic=str(last.get("epic") or ""),
            wait_reason=str(last.get("wait_reason") or ""),
            all_passed=bool(last.get("all_passed")),
            gates=list(last.get("gates") or []),
        )
    return {"by_epic": by_epic, "last": last}


def _market_quotes_from_ring(ring_tel: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in ring_tel.get("quote_ring") or []:
        if not isinstance(row, dict):
            continue
        epic = str(row.get("epic") or "")
        if not epic:
            continue
        try:
            bid = float(row.get("bid") or 0)
            offer = float(row.get("offer") or 0)
            mid = float(row.get("mid") or 0)
        except (TypeError, ValueError):
            bid = offer = mid = 0.0
        if mid <= 0 and bid > 0 and offer > 0:
            mid = (bid + offer) / 2.0
        out[epic] = {
            "epic": epic,
            "bid": bid,
            "offer": offer,
            "mid": mid,
            "last_price": mid,
            "source": str(row.get("source") or "ring"),
        }
    return out


def record_frontier_state(
    *,
    epic: str,
    coordinate: int,
    zone: int,
    lookup_ns: int,
    direction: str = "",
    rsi: float = 0.0,
    atr: float = 0.0,
    momentum: float = 0.0,
    win_zone: bool = False,
    all_passed: bool = False,
    injecting: bool = False,
    wait_reason: str = "",
    feed_race_us: dict[str, float] | None = None,
    strategy: dict[str, Any] | None = None,
) -> None:
    """Thread B — Alpha Frontier telemetry for executive console (no disk I/O)."""
    if injecting:
        valve = "🔥 FIRE: INJECTING LIVE IG ORDER"
    elif win_zone:
        valve = "🟢 WIN ZONE — Trading Enabled"
    else:
        valve = "⚪ SCANNING FRONTIER"
    row = {
        "epic": epic,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "coordinate": int(coordinate),
        "zone": int(zone),
        "zone_label": "WIN_ZONE" if win_zone else ("FAIL_ZONE" if zone == 0 else "UNMAPPED"),
        "lookup_ns": int(lookup_ns),
        "lookup_us": round(float(lookup_ns) / 1000.0, 3),
        "direction": direction,
        "vector": {
            "rsi": round(float(rsi), 2),
            "atr": round(float(atr), 4),
            "momentum": round(float(momentum), 5),
        },
        "execution_valve": valve,
        "injecting": bool(injecting),
        "all_passed": bool(all_passed),
        "wait_reason": str(wait_reason or ""),
        "feed_race_us": dict(feed_race_us or {}),
        "strategy": dict(strategy or {}),
    }
    with _CACHE_LOCK:
        _FRONTIER_STATE[str(epic)] = row
        _LAST_FRONTIER.clear()
        _LAST_FRONTIER.update(row)


def get_frontier_tracker_payload() -> dict[str, Any]:
    with _CACHE_LOCK:
        by_epic = dict(_FRONTIER_STATE)
        last = dict(_LAST_FRONTIER)
    ring_ft: dict[str, Any] = {}
    try:
        from system.ipc.ring_buffer import get_alpha_ring_buffer

        ring_ft = get_alpha_ring_buffer().frontier_tracker()
    except Exception:
        pass
    return {"by_epic": by_epic, "last": last, "ring": ring_ft}


def _write_pulse_snapshot(snap: dict[str, Any]) -> None:
    """Lightweight RAM/disk heartbeat — UI can never show stale frames."""
    global _PULSE_SERIAL
    _PULSE_SERIAL += 1
    envelope = {
        "serial": _PULSE_SERIAL,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "server_mono_ms": int(time.monotonic() * 1000),
        "refresh_ms": _REFRESH_MS,
        "payload": snap,
    }
    try:
        tmp = _PULSE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(envelope, default=str), encoding="utf-8")
        tmp.replace(_PULSE_PATH)
    except OSError:
        pass
    with _CACHE_LOCK:
        if _CACHE.get("mode") == "UNIFIED_FULFILLMENT":
            _CACHE["pulse_serial"] = _PULSE_SERIAL
            _CACHE["updated_at"] = envelope["updated_at"]


def read_pulse_snapshot() -> dict[str, Any] | None:
    try:
        if _PULSE_PATH.is_file():
            return json.loads(_PULSE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _ig_position_row(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map IG GET /positions OTC book entry → fulfillment performance row."""
    market = item.get("market") if isinstance(item.get("market"), dict) else {}
    position = item.get("position") if isinstance(item.get("position"), dict) else {}
    deal_id = str(position.get("dealId") or position.get("deal_id") or "").strip()
    if not deal_id:
        return None
    try:
        size = float(position.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    if size <= 0:
        return None
    epic = str(market.get("epic") or "").strip()
    direction = str(position.get("direction") or "").upper()
    try:
        entry = float(position.get("level") or market.get("bid") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        pnl_gbp = float(position.get("upl") or position.get("profit") or 0)
    except (TypeError, ValueError):
        pnl_gbp = 0.0
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return {
        "epic": epic,
        "direction": direction,
        "action": direction,
        "result": "OPEN",
        "confidence": 0.0,
        "cell_index": 0,
        "latency_us": 0.0,
        "deal_id": deal_id,
        "size": size,
        "entry": entry,
        "exit": 0.0,
        "pnl_gbp": round(pnl_gbp, 2),
        "executed_at": ts,
        "closed_at": "",
        "status": "OPEN",
        "source": "ig_rest_positions_otc",
    }


def sync_performance_rows_from_ig_rest(*, force: bool = False) -> int:
    """
    Replace phantom in-memory simulator rows with live IG OTC positions.

    Uses ``IGRestClient.open_positions()`` → GET ``/positions`` (OTC book).
    """
    global _IG_POSITIONS_SYNCED_AT
    now = time.monotonic()
    if not force and (now - _IG_POSITIONS_SYNCED_AT) < _IG_POSITIONS_SYNC_INTERVAL_SEC:
        return len(_PERF_ROWS)
    try:
        from system.agent_execution_mode import authentic_demo_broker_required

        if not authentic_demo_broker_required():
            return len(_PERF_ROWS)
    except Exception:
        return len(_PERF_ROWS)
    try:
        from system.credentials_loader import load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        rest = ensure_shared_authenticated(load_credentials())
        if not hasattr(rest, "open_positions"):
            return len(_PERF_ROWS)
        positions = rest.open_positions()
    except Exception:
        return len(_PERF_ROWS)

    rows: list[dict[str, Any]] = []
    for item in positions or []:
        if not isinstance(item, dict):
            continue
        mapped = _ig_position_row(item)
        if mapped:
            rows.append(mapped)
    with _CACHE_LOCK:
        _PERF_ROWS.clear()
        for row in rows[-128:]:
            _PERF_ROWS.append(row)
        if _PERF_ROWS:
            _CACHE["last_performance_row"] = _PERF_ROWS[-1]
        _CACHE["performance_rows"] = list(_PERF_ROWS)
    _IG_POSITIONS_SYNCED_AT = now
    return len(_PERF_ROWS)


def record_execution_performance_row(
    *,
    epic: str,
    direction: str,
    result: str,
    confidence: float,
    cell_index: int,
    latency_us: float,
    deal_id: str = "",
    size: float = 0.0,
    entry: float = 0.0,
    exit: float = 0.0,
    pnl_gbp: float = 0.0,
    status: str = "CLOSED",
    source: str = "",
) -> None:
    """Thread B — append execution row (in-memory only, no disk)."""
    src = str(source or "").strip().lower()
    if src in _PHANTOM_ROW_SOURCES:
        return
    deal = str(deal_id or "").strip()
    if not deal:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    row = {
        "epic": epic,
        "direction": direction,
        "action": direction,
        "result": str(result).upper(),
        "confidence": round(float(confidence), 2),
        "cell_index": int(cell_index),
        "latency_us": round(float(latency_us), 3),
        "deal_id": deal_id,
        "size": float(size),
        "entry": float(entry),
        "exit": float(exit),
        "pnl_gbp": round(float(pnl_gbp), 2),
        "executed_at": ts,
        "closed_at": ts if str(status).upper() == "CLOSED" else "",
        "status": str(status).upper(),
        "source": src or "ig_rest_positions_otc",
    }
    with _CACHE_LOCK:
        _PERF_ROWS.append(row)
        _CACHE["last_performance_row"] = row
        _CACHE["performance_rows"] = list(_PERF_ROWS)
    try:
        from analytics.post_open_audit import record_closed_trade

        record_closed_trade(row)
    except Exception:
        pass


def _attach_ui_stress_render(cache: dict[str, Any]) -> None:
    """Mirror active telemetry stress burst into fulfillment payload."""
    global _UI_STRESS_FLAG
    try:
        from intelligence.telemetry_daemon import ui_stress_test_active, ui_stress_test_status

        if ui_stress_test_active():
            st = ui_stress_test_status()
            _UI_STRESS_FLAG = {
                "active": True,
                "hz": float(st.get("hz") or 50.0),
                "epic": str(st.get("epic") or ""),
                "poll_ms": 20,
            }
            cache["ui_stress_render"] = dict(_UI_STRESS_FLAG)
            return
    except Exception:
        pass
    if _UI_STRESS_FLAG.get("active"):
        cache["ui_stress_render"] = dict(_UI_STRESS_FLAG)
    else:
        cache.pop("ui_stress_render", None)


def patch_fulfillment_stress_flag(
    *,
    active: bool,
    hz: float = 50.0,
    epic: str = "",
) -> None:
    """Expose UI stress mode to the Next.js terminal (20ms kinetic poll)."""
    global _UI_STRESS_FLAG
    with _CACHE_LOCK:
        if active:
            _UI_STRESS_FLAG = {
                "active": True,
                "hz": float(hz),
                "epic": str(epic or ""),
                "poll_ms": 20,
            }
            _CACHE["ui_stress_render"] = dict(_UI_STRESS_FLAG)
        else:
            _UI_STRESS_FLAG = {}
            _CACHE.pop("ui_stress_render", None)


def get_fulfillment_payload() -> dict[str, Any]:
    with _CACHE_LOCK:
        if _CACHE.get("mode") == "UNIFIED_FULFILLMENT":
            merged = dict(_CACHE)
            merged["performance_rows"] = list(_PERF_ROWS)
            if _PERF_ROWS:
                merged["last_performance_row"] = _PERF_ROWS[-1]
            merged["gate_diagnostics"] = get_gate_diagnostics_payload()
            merged["alpha_frontier_tracker"] = get_frontier_tracker_payload()
            merged["pulse_serial"] = int(_PULSE_SERIAL)
            merged["updated_at"] = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            merged["server_mono_ms"] = int(time.monotonic() * 1000)
            merged = _attach_live_market_quotes(merged)
            _attach_ui_stress_render(merged)
            return merged
    snap = _build_fulfillment_snapshot()
    _write_pulse_snapshot(snap)
    try:
        from system.ipc.ring_buffer import publish_cockpit_shm

        publish_cockpit_shm(snap)
    except Exception:
        pass
    _attach_ui_stress_render(snap)
    return snap


def _attach_live_market_quotes(payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh quote ring + gate diagnostics — never return a quote-starved envelope."""
    out = dict(payload)
    try:
        from system.ipc.ring_buffer import get_alpha_ring_buffer

        ring_tel = get_alpha_ring_buffer().telemetry()
        quotes = _market_quotes_from_ring(ring_tel)
        if quotes:
            out["market_quotes"] = quotes
            out["market_quotes_list"] = [
                {"epic": epic, **row} for epic, row in quotes.items()
            ]
        else:
            out.setdefault("market_quotes", {})
            out.setdefault("market_quotes_list", [])
    except Exception:
        out.setdefault("market_quotes", {})
        out.setdefault("market_quotes_list", [])
    try:
        gate = out.get("gate_diagnostics")
        if not isinstance(gate, dict):
            out["gate_diagnostics"] = get_gate_diagnostics_payload()
    except Exception:
        out["gate_diagnostics"] = {"by_epic": {}, "last": {}}
    return out


def _ingestion_stage(feed: dict[str, Any]) -> dict[str, Any]:
    active = feed.get("active_feeds") or []
    count = len(active)
    if count >= 3:
        label = "🟢 Yahoo + Finnhub + Twelve Data Resilient"
        ok = True
    elif count >= 1:
        label = f"🟡 Partial feed resilience ({count}/3)"
        ok = False
    else:
        label = "🔴 Feed hub offline"
        ok = False
    return {"id": 1, "name": "Ingestion Health", "label": label, "ok": ok}


def _matrix_stage(ring_tel: dict[str, Any], matrix_tel: dict[str, Any]) -> dict[str, Any]:
    density = int(ring_tel.get("vector_density") or 0)
    patterns = int(matrix_tel.get("patterns_scanned") or 43200)
    live = int(ring_tel.get("live_ticks_cached") or 0)
    ticks = patterns + live
    if density > 0 or ticks > 0:
        label = f"🟢 {ticks:,} Ticks Cached in RAM"
        ok = True
    else:
        label = "🟡 Look-Ahead Matrix compiling"
        ok = False
    return {
        "id": 2,
        "name": "Look-Ahead Matrix",
        "label": label,
        "ok": ok,
        "vector_density": density,
        "ticks_cached": ticks,
    }


def _calibration_stage(ring_tel: dict[str, Any]) -> dict[str, Any]:
    aligned = bool(ring_tel.get("thread_aligned"))
    if aligned:
        label = "🟢 Edge Delta Calibrated"
        ok = True
    else:
        label = "🟡 Auto-Tuning Core warming"
        ok = False
    return {"id": 3, "name": "Auto-Tuning Core", "label": label, "ok": ok}


def _execution_stage(threads: dict[str, Any], ring_tel: dict[str, Any]) -> dict[str, Any]:
    b_alive = bool(threads.get("b_alive"))
    primed = b_alive and int(ring_tel.get("compile_generation") or 0) > 0
    if primed:
        label = "🟢 INJECTION CORE PRIMED FOR OPEN"
        ok = True
    elif b_alive:
        label = "🟡 Execution bridge arming"
        ok = False
    else:
        label = "🔴 Live execution bridge offline"
        ok = False
    return {"id": 4, "name": "Live Execution Bridge", "label": label, "ok": ok}


def _build_fulfillment_snapshot() -> dict[str, Any]:
    feed: dict[str, Any] = {}
    ring_tel: dict[str, Any] = {}
    matrix_tel: dict[str, Any] = {}
    threads: dict[str, Any] = {}
    try:
        from system.feeds.multi_feed_hub import feed_hub_telemetry

        feed = feed_hub_telemetry()
    except Exception:
        feed = {}
    try:
        from system.ipc.ring_buffer import get_alpha_ring_buffer

        ring_tel = get_alpha_ring_buffer().telemetry()
    except Exception:
        ring_tel = {}
    try:
        from intelligence.matrix_prebaker import matrix_compiler_telemetry

        matrix_tel = matrix_compiler_telemetry()
    except Exception:
        matrix_tel = {}
    try:
        from system.unified_engine import unified_thread_state

        threads = unified_thread_state()
    except Exception:
        threads = {}

    stages = [
        _ingestion_stage(feed),
        _matrix_stage(ring_tel, matrix_tel),
        _calibration_stage(ring_tel),
        _execution_stage(threads, ring_tel),
    ]
    matrix_stage = stages[1] if len(stages) > 1 else {}
    velocity_metrics = _extract_velocity_metrics(
        matrix_stage=matrix_stage,
        feed=feed,
        ring_tel=ring_tel,
    )
    data_velocity = _update_data_velocity_watchdog(velocity_metrics)
    gate_payload = get_gate_diagnostics_payload()
    tuning = _tuning_variables(ring_tel, gate_payload)
    frontier = get_frontier_tracker_payload()
    traffic = _traffic_light_hub(feed, ring_tel, frontier, threads, data_velocity)
    traffic, stages = _apply_velocity_overrides(traffic, data_velocity, stages)
    try:
        from runtime.dual_core_execution import dual_core_status_dict, refresh_dual_core_from_hub

        refresh_dual_core_from_hub()
        dual_core = dual_core_status_dict()
    except Exception:
        dual_core = {}
    return {
        "mode": "UNIFIED_FULFILLMENT",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "refresh_ms": _REFRESH_MS,
        "stages": stages,
        "traffic_light_hub": traffic,
        "data_velocity": data_velocity,
        "trading_paused": bool(data_velocity.get("trading_paused")),
        "critical_alert": data_velocity.get("critical_alert"),
        "ticks_cached": int(data_velocity.get("ticks_cached") or 0),
        "all_ready": all(s.get("ok") for s in stages) and not data_velocity.get("stall_active"),
        "stream_mapping_banner": feed.get(
            "stream_mapping_banner",
            "🟢 Yahoo + Finnhub + Twelve Data Mapped (Absolute Feed Resilience)",
        ),
        "performance_rows": list(_PERF_ROWS),
        "last_performance_row": _PERF_ROWS[-1] if _PERF_ROWS else None,
        "e2e_latency_ns": ring_tel.get("e2e_latency_ns") or {},
        "tuning_variables": tuning,
        "alpha_frontier_tracker": frontier,
        "gate_diagnostics": gate_payload,
        "market_quotes": _market_quotes_from_ring(ring_tel),
        "memory_alignment": "TRUE SYNC" if bool(ring_tel.get("thread_aligned")) else "WARMING",
        "execution_mode": dual_core.get("execution_mode"),
        "volatility_z_score": dual_core.get("volatility_z_score"),
        "dual_core_status": dual_core,
    }


def _traffic_light_hub(
    feed: dict[str, Any],
    ring_tel: dict[str, Any],
    frontier: dict[str, Any],
    threads: dict[str, Any],
    data_velocity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = len(feed.get("active_feeds") or [])
    live_ticks = int((data_velocity or {}).get("live_ram_ticks") or 0)
    velocity_ok = live_ticks > 0 or int((data_velocity or {}).get("race_wins_total") or 0) > 0
    ingestion_ok = active >= 1 and velocity_ok and not bool((data_velocity or {}).get("stall_active"))
    ring_ft = frontier.get("ring") or ring_tel.get("alpha_frontier") or {}
    last_ft = frontier.get("last") or {}
    coord = last_ft.get("coordinate") or ring_ft.get("last_coordinate") or 0
    injecting = bool(last_ft.get("injecting"))
    win_zone = (
        last_ft.get("zone_label") == "WIN_ZONE"
        or ring_ft.get("last_zone_label") == "WIN_ZONE"
        or bool(ring_ft.get("win_zone_armed"))
    )
    b_alive = bool(threads.get("b_alive"))
    return {
        "ingestion": {
            "color": "green" if ingestion_ok else "red",
            "icon": "🟢" if ingestion_ok else "🔴",
            "label": "Ingestion Health",
            "detail": (
                f"{active}/3 feeds · {live_ticks:,} live RAM ticks"
                if ingestion_ok
                else f"{active}/3 feeds — NO live tick velocity"
            ),
            "live_ram_ticks": live_ticks,
            "ticks_cached": int((data_velocity or {}).get("ticks_cached") or 0),
        },
        "matrix": {
            "color": "blue",
            "icon": "🔵",
            "label": "Live Matrix Coordinates",
            "coordinate": int(coord),
            "total_cells": int(ring_ft.get("total_cells") or 131072),
        },
        "execution": {
            "color": "fire" if injecting else ("green" if win_zone else "neutral"),
            "icon": "🔥" if injecting else ("🟢" if win_zone else "⚪"),
            "label": "Master Execution Valve",
            "detail": (
                "🔥 FIRE: INJECTING LIVE IG ORDER"
                if injecting
                else ("WIN ZONE" if win_zone else "⚪ SCANNING FRONTIER")
            ),
        },
        "thread_b_alive": b_alive,
    }


def _tuning_variables(
    ring_tel: dict[str, Any],
    gate_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    live = dict(ring_tel.get("live_tuning") or {})
    gate_payload = gate_payload or {}
    for row in (gate_payload.get("by_epic") or {}).values():
        tun = row.get("tuning") or {}
        if tun:
            live.update({k: v for k, v in tun.items() if v is not None})
        for gate in row.get("gates") or []:
            if gate.get("name") != "signal_confidence":
                continue
            val = gate.get("value") or {}
            lt = val.get("live_threshold") or val.get("floor") or val.get("threshold")
            if lt is not None:
                try:
                    live["signal_threshold"] = float(lt)
                except (TypeError, ValueError):
                    pass
            break
    signal_threshold = float(live.get("signal_threshold") or ring_tel.get("signal_threshold") or 0)
    atr_multiplier = float(live.get("atr_multiplier") or ring_tel.get("atr_multiplier") or 0)
    if atr_multiplier <= 0:
        for row in (gate_payload.get("by_epic") or {}).values():
            vec = (row.get("vector") or {}).get("atr")
            if vec:
                try:
                    atr_multiplier = max(float(vec) / 1.5, 0.5)
                    break
                except (TypeError, ValueError):
                    pass
    if signal_threshold <= 0 or atr_multiplier <= 0:
        try:
            from system.config_loader import get_config

            cfg = get_config()
            if signal_threshold <= 0:
                signal_threshold = float(cfg.signal_threshold)
            if atr_multiplier <= 0:
                atr_multiplier = float(
                    getattr(cfg, "adaptive_atr_risk_multiple", None)
                    or getattr(cfg, "atr_multiplier", None)
                    or 2.5
                )
        except Exception:
            pass
    if signal_threshold <= 0:
        signal_threshold = 52.5
    return {
        "signal_threshold": round(signal_threshold, 2),
        "atr_multiplier": round(atr_multiplier, 3),
        "win_trajectory_relax_pts": round(
            float(live.get("win_trajectory_relax_pts") or 5.0), 2
        ),
        "source": "live_gate" if live.get("signal_threshold") else "config",
    }


def _refresh_loop() -> None:
    while not _REFRESH_STOP.wait(_REFRESH_MS / 1000.0):
        try:
            sync_performance_rows_from_ig_rest()
            snap = _build_fulfillment_snapshot()
            with _CACHE_LOCK:
                gate_keep = {"by_epic": dict(_GATE_DIAG), "last": dict(_LAST_GATE_DIAG)}
                frontier_keep = get_frontier_tracker_payload()
                perf = list(_PERF_ROWS)
                _CACHE.clear()
                _CACHE.update(snap)
                _CACHE["gate_diagnostics"] = gate_keep
                _CACHE["alpha_frontier_tracker"] = frontier_keep
                _CACHE["performance_rows"] = perf
                if perf:
                    _CACHE["last_performance_row"] = perf[-1]
                _attach_ui_stress_render(_CACHE)
                pulse = dict(_CACHE)
                pulse["gate_diagnostics"] = gate_keep
            _write_pulse_snapshot(pulse)
            try:
                from system.ipc.ring_buffer import publish_cockpit_shm

                publish_cockpit_shm(snap)
            except Exception:
                pass
        except Exception:
            pass


def start_fulfillment_cache_refresh() -> None:
    global _REFRESH_THREAD
    if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
        return
    _REFRESH_STOP.clear()
    sync_performance_rows_from_ig_rest(force=True)
    snap = _build_fulfillment_snapshot()
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(snap)
    _write_pulse_snapshot(snap)
    try:
        from system.ipc.ring_buffer import publish_cockpit_shm

        publish_cockpit_shm(snap)
    except Exception:
        pass
    _REFRESH_THREAD = threading.Thread(
        target=_refresh_loop,
        name="unified-fulfillment-cache",
        daemon=True,
    )
    _REFRESH_THREAD.start()


def stop_fulfillment_cache_refresh() -> None:
    _REFRESH_STOP.set()


def force_cockpit_feed_heal(*, reason: str = "operator") -> dict[str, Any]:
    """
    Self-heal entry — hard-reset feeds, rebuild fulfillment snapshot, re-publish SHM.
    Safe to call from desktop cockpit stall guardian or POST /api/cockpit/heal.
    """
    feed_reset = _trigger_feed_hard_reset(reason=f"cockpit_heal:{reason}")
    snap = _build_fulfillment_snapshot()
    with _CACHE_LOCK:
        gate_keep = {"by_epic": dict(_GATE_DIAG), "last": dict(_LAST_GATE_DIAG)}
        frontier_keep = get_frontier_tracker_payload()
        perf = list(_PERF_ROWS)
        _CACHE.clear()
        _CACHE.update(snap)
        _CACHE["gate_diagnostics"] = gate_keep
        _CACHE["alpha_frontier_tracker"] = frontier_keep
        _CACHE["performance_rows"] = perf
        if perf:
            _CACHE["last_performance_row"] = perf[-1]
    try:
        from system.ipc.ring_buffer import publish_cockpit_shm

        publish_cockpit_shm(snap)
    except Exception:
        pass
    try:
        from system.engine_log import log_engine

        log_engine(f"cockpit_feed_heal: reason={reason} feed_reset={bool(feed_reset)}")
    except Exception:
        pass
    dv = snap.get("data_velocity") or {}
    return {
        "ok": True,
        "reason": reason,
        "feed_reset": feed_reset,
        "stall_active": bool(dv.get("stall_active")),
        "write_seq_hint": "republished",
        "agent_pid": os.getpid(),
    }


def heal_epic_execution_gateway(epic: str) -> bool:
    """Clear stale overdue execution blocks from fulfillment gate diagnostics."""
    key = str(epic or "").strip()
    if not key:
        return False
    healed = False
    with _CACHE_LOCK:
        row = _GATE_DIAG.get(key)
        if isinstance(row, dict):
            wait = str(row.get("wait_reason") or "").lower()
            if "overdue" in wait or "confirmation" in wait or "in progress" in wait:
                patched = dict(row)
                patched["wait_reason"] = ""
                patched["accepting_ticks"] = True
                patched["healed_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                )
                _GATE_DIAG[key] = patched
                healed = True
        frontier = _CACHE.get(key)
        if isinstance(frontier, dict):
            wait = str(frontier.get("wait_reason") or "").lower()
            if "overdue" in wait or "confirmation" in wait:
                patched = dict(frontier)
                patched["wait_reason"] = ""
                patched["accepting_ticks"] = True
                _CACHE[key] = patched
                healed = True
    return healed


def reset_fulfillment_cache_for_tests() -> None:
    stop_fulfillment_cache_refresh()
    global _REFRESH_THREAD, _IG_POSITIONS_SYNCED_AT
    _REFRESH_THREAD = None
    _IG_POSITIONS_SYNCED_AT = 0.0
    with _CACHE_LOCK:
        _CACHE.clear()
        _PERF_ROWS.clear()
        _GATE_DIAG.clear()
        _LAST_GATE_DIAG.clear()
    with _VELOCITY_LOCK:
        _VELOCITY_STATE.update(
            {
                "last_ticks_cached": None,
                "last_live_ram_ticks": None,
                "last_race_wins": None,
                "last_change_mono": 0.0,
                "stalled_since_mono": None,
                "stall_active": False,
                "last_reset_mono": 0.0,
                "reset_count": 0,
            }
        )
