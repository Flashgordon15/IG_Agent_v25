"""
Lightweight system health snapshot — O(1) HTTP response, <5ms target.

All expensive reads happen in a background thread every 1s.  The HTTP handler
returns a dict copy of the in-memory snapshot without calling any external APIs,
heavy endpoints, or build_gui_status.

Fields:
  agent_online           – always True if handler executes
  execution_loop_active  – dual_core rotation sweep count is increasing
  routing_state          – {armed: int, degraded: bool, none: int}
  feed_heartbeat_age_ms  – min age (ms) across active stack from _last_fresh_tick_at
  ws_state               – {connected: bool, degraded: bool, reconnecting: bool}
  cached_api_latency_ms  – rolling p50 for request:health from endpoint_profiler
  ig_available           – lightweight cached check (refreshed every 30s background)
  yahoo_available        – lightweight cached check (refreshed every 30s background)
  data_feeds             – per-feed status (hub, yahoo, rest_stream)
  heartbeat_ts           – ISO timestamp of last snapshot
  heartbeat_mono         – monotonic clock of last snapshot
  agent_version          – APP_VERSION_LABEL from app_identity
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_HEARTBEAT_FILE = Path("src/data/health_light_heartbeat.json")
_REFRESH_INTERVAL_SEC = 1.0
_PROVIDER_RECHECK_SEC = 30.0

_lock = threading.Lock()
_snapshot: dict[str, Any] = {
    "agent_online": True,
    "execution_loop_active": False,
    "routing_state": {"armed": 0, "degraded": False, "none": 0},
    "feed_heartbeat_age_ms": None,
    "ws_state": {"connected": False, "degraded": False, "reconnecting": False},
    "cached_api_latency_ms": None,
    "ig_available": None,
    "yahoo_available": None,
    "data_feeds": {},
    "heartbeat_ts": "",
    "heartbeat_mono": 0.0,
    "agent_version": "",
}

_refresher_thread: threading.Thread | None = None
_refresher_stop = threading.Event()

# Background-only availability checks (not on HTTP path)
_last_provider_check: float = 0.0
_last_sweep_count: int = 0
_last_sweep_ts: float = 0.0


def get_health_light_response() -> dict[str, Any]:
    """O(1) dict copy — MUST NOT call external APIs or heavy endpoints."""
    with _lock:
        return dict(_snapshot)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _refresh_snapshot() -> None:
    """Called every 1s from background thread — reads ONLY cached/local sources."""
    global _last_provider_check, _last_sweep_count, _last_sweep_ts

    now = time.time()
    snap: dict[str, Any] = {
        "agent_online": True,
        "heartbeat_ts": _utc_now_iso(),
        "heartbeat_mono": round(now, 3),
    }

    # Agent version
    try:
        from system.app_identity import APP_VERSION_LABEL

        snap["agent_version"] = APP_VERSION_LABEL
    except Exception:
        snap["agent_version"] = ""

    # Execution loop active — stacked thread alive + tpm or sweep advancing
    try:
        from runtime.dual_core_execution import (
            _ensure_stacked_sweep_running,
            _ticks_per_minute,
            get_active_stack_epics,
            get_rotation_state,
            is_stacked_sweep_thread_alive,
        )

        stack = get_active_stack_epics()
        state = get_rotation_state()
        sweep = int(state.get("rotation_sweep_count") or 0)
        if sweep > _last_sweep_count:
            _last_sweep_count = sweep
            _last_sweep_ts = now
        elapsed_since_sweep = now - _last_sweep_ts if _last_sweep_ts > 0 else 9999
        min_tpm = min((_ticks_per_minute(e) for e in stack), default=0) if stack else 0
        thread_ok = is_stacked_sweep_thread_alive()
        if not thread_ok:
            _ensure_stacked_sweep_running()
            thread_ok = is_stacked_sweep_thread_alive()
        elif elapsed_since_sweep > 30.0 and min_tpm > 0:
            _ensure_stacked_sweep_running()
            thread_ok = is_stacked_sweep_thread_alive()
        snap["execution_loop_active"] = thread_ok and (
            elapsed_since_sweep < 5.0 or min_tpm >= 5
        )
        snap["rotation_sweep_count"] = sweep
        snap["stacked_sweep_alive"] = thread_ok
    except Exception:
        snap["execution_loop_active"] = False
        snap["rotation_sweep_count"] = 0
        snap["stacked_sweep_alive"] = False

    # Routing state — cached unified routes (not gui_status cache)
    try:
        from runtime.unified_execution import cached_unified_routes

        routes = cached_unified_routes() or []
        if routes:
            armed = sum(
                1
                for r in routes
                if isinstance(r, dict)
                and str(r.get("execution_path") or "NONE").upper() != "NONE"
            )
            none_count = len(routes) - armed
            degraded = armed == 0 and len(routes) > 0
        else:
            armed = 0
            none_count = 0
            degraded = False
        snap["routing_state"] = {"armed": armed, "degraded": degraded, "none": none_count}
    except Exception:
        snap["routing_state"] = {"armed": 0, "degraded": False, "none": 0}

    # Feed heartbeat age — min age across active stack
    try:
        from runtime.dual_core_execution import get_active_stack_epics, get_socket_heartbeat_state

        stack = get_active_stack_epics()
        hb = get_socket_heartbeat_state()
        last_ticks = hb.get("last_fresh_tick_at") or {}
        ages_ms: list[float] = []
        for epic in stack:
            ts = last_ticks.get(epic)
            if ts and ts > 0:
                ages_ms.append((now - float(ts)) * 1000.0)
        snap["feed_heartbeat_age_ms"] = round(min(ages_ms), 1) if ages_ms else None
    except Exception:
        snap["feed_heartbeat_age_ms"] = None

    # WS state — from state_ws subscriber count
    try:
        from api.state_ws import get_ws_subscriber_count

        count = int(get_ws_subscriber_count() or 0)
        snap["ws_state"] = {
            "connected": count > 0,
            "degraded": False,
            "reconnecting": False,
        }
    except Exception:
        snap["ws_state"] = {"connected": False, "degraded": False, "reconnecting": False}

    # Cached API latency — p50 for request:health
    try:
        from api.endpoint_profiler import timing_summary

        timings = timing_summary()
        health_timing = timings.get("request:health")
        if isinstance(health_timing, dict):
            snap["cached_api_latency_ms"] = health_timing.get("p50_ms")
        else:
            snap["cached_api_latency_ms"] = None
    except Exception:
        snap["cached_api_latency_ms"] = None

    # Data feeds — cached hub status per feed
    try:
        from system.market_data_hub import get_market_data_hub, NIGHT_MATRIX_EPICS

        hub = get_market_data_hub()
        fresh_count = 0
        for epic in NIGHT_MATRIX_EPICS:
            q = hub.get_snapshot(epic)
            if q is not None and float(getattr(q, "bid", 0) or 0) > 0 and q.age_seconds() <= 45.0:
                fresh_count += 1
        snap["data_feeds"] = {
            "hub": {"fresh_count": fresh_count, "total": len(NIGHT_MATRIX_EPICS)},
        }
    except Exception:
        snap["data_feeds"] = {}

    # Rotation / feed stall telemetry (P2) — cached dual-core state only
    try:
        from runtime.dual_core_execution import (
            _ticks_per_minute,
            get_active_stack_epics,
            get_rotation_state,
        )

        stack = get_active_stack_epics()
        rot = get_rotation_state()
        snap["feed_stall"] = bool(stack) and all(_ticks_per_minute(e) == 0 for e in stack)
        snap["rotation_escape_active"] = bool(rot.get("rotation_escape_active"))
        snap["last_rotation_reason"] = str(rot.get("last_rotation_reason") or "")
        snap["stack_tpm"] = {e: _ticks_per_minute(e) for e in stack}
        snap["boot_grace_active"] = bool(rot.get("boot_grace_active"))
        from runtime.dual_core_execution import get_z_score_stream

        snap["z_stream_lengths"] = {
            e: len(get_z_score_stream(e)) for e in stack
        }
    except Exception:
        snap["feed_stall"] = False
        snap["rotation_escape_active"] = False
        snap["last_rotation_reason"] = ""
        snap["stack_tpm"] = {}
        snap["boot_grace_active"] = False

    # Provider availability — only refresh every 30s (background, not on HTTP)
    if now - _last_provider_check >= _PROVIDER_RECHECK_SEC:
        _last_provider_check = now
        _refresh_provider_availability(snap)
    else:
        with _lock:
            snap["ig_available"] = _snapshot.get("ig_available")
            snap["yahoo_available"] = _snapshot.get("yahoo_available")

    with _lock:
        _snapshot.update(snap)

    try:
        from system.unified_runtime_state import update_from_health_light

        update_from_health_light(snap)
    except Exception:
        pass

    # Write heartbeat file
    _write_heartbeat_file()


def _refresh_provider_availability(snap: dict[str, Any]) -> None:
    """Lightweight availability checks — hub has recent quote or last yahoo success."""
    now = time.time()

    # IG available — hub has at least one fresh quote within 60s
    try:
        from system.market_data_hub import get_market_data_hub, NIGHT_MATRIX_EPICS

        hub = get_market_data_hub()
        ig_ok = any(
            hub.get_snapshot(epic) is not None
            and float(getattr(hub.get_snapshot(epic), "bid", 0) or 0) > 0
            and hub.get_snapshot(epic).age_seconds() <= 60.0
            for epic in NIGHT_MATRIX_EPICS
        )
        snap["ig_available"] = ig_ok
    except Exception:
        snap["ig_available"] = None

    # Yahoo available — transport yahoo or recent hub/yahoo-sourced ticks
    try:
        from feeder.pricing_transport import reference_transport_is_yahoo
        from runtime.dual_core_execution import _tick_arrivals, ROTATION_UNIVERSE

        cfg = None
        try:
            from system.config_loader import ConfigLoader

            cfg = ConfigLoader().load(validate=False)
        except Exception:
            pass
        transport_yahoo = reference_transport_is_yahoo(cfg) if cfg is not None else False
        any_recent = any(
            len(arr) > 0 and (now - max(arr)) < 60.0
            for epic in ROTATION_UNIVERSE
            if (arr := _tick_arrivals.get(epic)) is not None
        )
        snap["yahoo_available"] = bool(transport_yahoo or any_recent)
    except Exception:
        snap["yahoo_available"] = None


def _write_heartbeat_file() -> None:
    """Write lightweight heartbeat to disk every 1s for supervisor monitoring."""
    try:
        import os

        pid = os.getpid()
        session_id = ""
        try:
            from runtime.session_identity import get_session_id

            session_id = str(get_session_id() or "")
        except Exception:
            pass
        payload = {
            "ts": _utc_now_iso(),
            "pid": pid,
            "session_id": session_id,
        }
        _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(_HEARTBEAT_FILE)
    except Exception:
        pass


def _refresher_loop() -> None:
    while not _refresher_stop.wait(_REFRESH_INTERVAL_SEC):
        try:
            _refresh_snapshot()
        except Exception as exc:
            log_engine(f"HealthLight: refresher error {type(exc).__name__}: {exc}")


def start_health_light_refresher() -> None:
    """Start background 1s refresher thread (idempotent)."""
    global _refresher_thread
    if _refresher_thread is not None and _refresher_thread.is_alive():
        return
    _refresher_stop.clear()
    _refresher_thread = threading.Thread(
        target=_refresher_loop,
        name="health-light-refresher",
        daemon=True,
    )
    _refresher_thread.start()
    # Seed immediately so first HTTP request is non-empty
    try:
        _refresh_snapshot()
    except Exception:
        pass
    log_engine("HealthLight: 1s refresher started")


def stop_health_light_refresher() -> None:
    _refresher_stop.set()
