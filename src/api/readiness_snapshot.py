"""Async readiness snapshots for fast /api/health and /api/gui_status serving.

Heavy health and GUI payloads are rebuilt on a background daemon thread.
HTTP handlers read pre-built snapshots only — never block on advisory chains.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

from api.gate_health_matrix import _gate_status
from system.system_state import GateStatus, get_system_state

_HEALTH_REFRESH_INTERVAL_SEC = 5.0
_GUI_REFRESH_INTERVAL_SEC = 4.0
_GUI_FAST_INTERVAL_SEC = 2.0
_GUI_FULL_INTERVAL_SEC = 10.0
_LOOP_TICK_SEC = 0.25

_HEALTH_SNAPSHOT: dict[str, Any] = {}
_GUI_SNAPSHOT: dict[str, Any] = {}
_GUI_LAYERS: dict[str, dict[str, Any]] = {"fast": {}, "full": {}}
_META: dict[str, Any] = {
    "health_ts": 0.0,
    "gui_ts": 0.0,
    "gui_fast_ts": 0.0,
    "gui_full_ts": 0.0,
    "gui_refreshing": False,
    "gui_full_refreshing": False,
}
_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def resolve_gate_progression() -> dict[str, Any]:
    """Fast O(1) gate progression from SystemState — safe on request threads."""
    snap = get_system_state().snapshot()
    gates_raw = snap.get("gates") or {}
    gates = {
        gid: _gate_status(gates_raw, gid)
        for gid in ("G1", "G2", "G3", "G4", "G5")
    }
    phase = str(snap.get("phase") or "BOOTING").upper()
    active_gate: int | None = None
    for idx, gid in enumerate(("G1", "G2", "G3", "G4", "G5"), start=1):
        st = gates.get(gid) or str(GateStatus.PENDING)
        if st in (str(GateStatus.PENDING), str(GateStatus.RUNNING)):
            active_gate = idx
            break
    return {
        "phase": phase,
        "label": str(snap.get("phase_label") or snap.get("label") or ""),
        "percent": snap.get("percent"),
        "ready": bool(snap.get("ready")),
        "gates": gates,
        "active_gate": active_gate,
        "warm_up_complete": gates.get("G5") == str(GateStatus.COMPLETE)
        or phase in ("READY", "G5"),
        "operational_ready": gates.get("G3") == str(GateStatus.COMPLETE),
    }


def _derive_matrix_fields(prog: dict[str, Any]) -> dict[str, Any]:
    """Informational boot status — mirrors gate matrix semantics without blocking HTTP."""
    gates = prog.get("gates") or {}
    g1 = gates.get("G1", str(GateStatus.PENDING))
    g2 = gates.get("G2", str(GateStatus.PENDING))
    g3 = gates.get("G3", str(GateStatus.PENDING))
    phase = str(prog.get("phase") or "BOOTING").upper()

    if phase == "FAILED" or any(
        gates.get(gid) == str(GateStatus.FAILED) for gid in ("G1", "G2", "G3")
    ):
        active_gate = (
            3
            if g3 == str(GateStatus.FAILED)
            else 2
            if g2 == str(GateStatus.FAILED)
            else 1
        )
        snap = get_system_state().snapshot()
        return {
            "status": "FAILED",
            "gate": active_gate,
            "ready": False,
            "detail": str(snap.get("error") or snap.get("phase_label") or "boot failed"),
        }

    if g3 == str(GateStatus.COMPLETE):
        return {"status": "OPERATIONAL", "ready": True}

    if g1 == str(GateStatus.COMPLETE):
        return {"status": "HYDRATING", "gate": 2, "ready": False}

    return {"status": "INITIALIZING", "gate": 1, "ready": False}


def _http_code_for_body(body: dict[str, Any]) -> int:
    """Return 503 only for FAILED; warm-up phases stay 200 so launchers can poll."""
    if str(body.get("status") or "").upper() == "FAILED":
        return 503
    return 200


def _overlay_live_iron_cage(body: dict[str, Any]) -> dict[str, Any]:
    """Align /api/health trade_ready with the live health_light plane (O(1))."""
    try:
        from api.health_light import get_health_light_response, iron_cage_from_health_light_snapshot

        hl = get_health_light_response()
        if not hl:
            return body
        ic = iron_cage_from_health_light_snapshot(hl)
        iron = body.get("iron_cage") if isinstance(body.get("iron_cage"), dict) else {}
        stale_source = str(iron.get("source") or "") in ("peek_empty", "")
        if ic.get("trade_ready") or stale_source or not iron:
            hub = (hl.get("data_feeds") or {}).get("hub") or {}
            body["iron_cage"] = {
                **ic,
                "execution": {
                    "loop_active": bool(hl.get("execution_loop_active")),
                    "stacked_sweep_alive": bool(hl.get("stacked_sweep_alive")),
                    "rotation_sweep_count": int(hl.get("rotation_sweep_count") or 0),
                    "routes_armed": int((hl.get("routing_state") or {}).get("armed") or 0),
                },
                "feeds": {
                    "health": "ok" if int(hub.get("fresh_count") or 0) >= 1 else "offline",
                    "fresh_count": int(hub.get("fresh_count") or 0),
                    "total_epics": int(hub.get("total") or 0),
                },
                "ts": time.time(),
                "source": "health_light_overlay",
            }
            trade_ready = bool(ic.get("trade_ready"))
            body["trade_ready"] = trade_ready
            if trade_ready:
                body["trading_loops_running"] = bool(hl.get("execution_loop_active"))
                body["trading_healthy"] = True
                issues = [
                    x
                    for x in (body.get("issues") or [])
                    if not str(x).startswith("iron_cage:")
                    and x not in ("trading_loops_not_running", "no_gate_activity_recorded")
                ]
                body["issues"] = issues
                body["ok"] = bool(body.get("watchdog_active", True)) and bool(
                    body.get("supervision_drift_ok", True)
                    if body.get("supervision_drift_ok") is not None
                    else True
                )
            try:
                from system.iron_cage_readiness import publish_operational_iron_cage_cache

                if trade_ready:
                    publish_operational_iron_cage_cache(body["iron_cage"])
            except Exception:
                pass
    except Exception:
        pass
    return body


def _compose_health_light() -> dict[str, Any]:
    """Cheap health body when the background snapshot has not warmed yet."""
    from api.readiness_model import build_readiness_from_system_state

    prog = resolve_gate_progression()
    body: dict[str, Any] = {
        **_derive_matrix_fields(prog),
        "gate_progression": prog,
        "snapshot_warming": True,
        **build_readiness_from_system_state(),
    }
    try:
        from runtime.session_identity import build_session_identity_fields

        body.update(build_session_identity_fields())
    except Exception:
        pass
    try:
        from api.agent_health import get_cached_health_status

        cached = get_cached_health_status(allow_slow_fallback=False)
        if isinstance(cached, dict):
            body = {**cached, **body}
    except Exception:
        pass
    return body


def refresh_health_snapshot() -> dict[str, Any]:
    """Rebuild health snapshot (background thread only)."""
    from api.endpoint_profiler import timed_section

    global _HEALTH_SNAPSHOT
    with timed_section("health.snapshot_refresh"):
        try:
            from api.agent_health import refresh_health_cache

            refresh_health_cache()
        except Exception:
            pass

        prog = resolve_gate_progression()
        matrix = _derive_matrix_fields(prog)
        body: dict[str, Any] = {**matrix, "gate_progression": prog, "snapshot_warming": False}

        try:
            from api.readiness_model import build_readiness_from_system_state

            body.update(build_readiness_from_system_state())
        except Exception:
            pass

        # session_identity is already included in the cached health payload from
        # refresh_health_cache() above — skip the redundant call here to avoid the
        # 3-second health_endpoint_ok self-call that caused p95=6656ms spikes.

        try:
            from api.v31_telemetry import resolve_risk_tracking_fields

            body.update(resolve_risk_tracking_fields())
        except Exception:
            pass

        try:
            from api.agent_health import get_cached_health_status

            cached = get_cached_health_status(allow_slow_fallback=False)
            if isinstance(cached, dict):
                body = {**cached, **body}
        except Exception:
            pass

        try:
            from api.endpoint_profiler import timing_summary

            body["endpoint_profile"] = timing_summary()
        except Exception:
            pass

        body = _overlay_live_iron_cage(body)

    global _HEALTH_SNAPSHOT
    _HEALTH_SNAPSHOT = body
    with _LOCK:
        _META["health_ts"] = time.time()
    return body


def _publish_gui_merged() -> dict[str, Any]:
    """Merge fast + full GUI layers into served snapshot."""
    fast = dict(_GUI_LAYERS.get("fast") or {})
    full = dict(_GUI_LAYERS.get("full") or {})
    merged = {**fast, **full}
    if full:
        merged["snapshot_tier"] = "full"
        merged["snapshot_warming"] = False
    else:
        merged["snapshot_tier"] = "fast"
        merged["snapshot_warming"] = not bool(fast)
    merged["refreshing"] = bool(_META.get("gui_refreshing") or _META.get("gui_full_refreshing"))
    with _LOCK:
        global _GUI_SNAPSHOT
        _GUI_SNAPSHOT = merged
        _META["gui_ts"] = time.time()
    return merged


def refresh_gui_fast_snapshot() -> dict[str, Any]:
    """Cheap GUI slice — feeds, pipeline, governance shell."""
    from api.endpoint_profiler import timed_section
    from api.gui_status_fast import build_gui_status_fast

    with timed_section("gui_status.refresh_fast"):
        try:
            fast = build_gui_status_fast()
            fast["refreshing"] = bool(_META.get("gui_full_refreshing"))
        except Exception:
            fast = dict(_GUI_LAYERS.get("fast") or {})
    with _LOCK:
        _GUI_LAYERS["fast"] = fast
        _META["gui_fast_ts"] = time.time()
    merged = _publish_gui_merged()
    _sync_gui_status_cache(merged)
    return merged


def refresh_gui_full_snapshot() -> dict[str, Any]:
    """Full advisory GUI rebuild (background only)."""
    from api.endpoint_profiler import timed_section

    global _GUI_SNAPSHOT
    with _LOCK:
        if _META.get("gui_full_refreshing"):
            return dict(_GUI_SNAPSHOT)
        _META["gui_full_refreshing"] = True

    try:
        with timed_section("gui_status.refresh_full"):
            from api.gui_status import build_gui_status

            fresh = build_gui_status()
            fresh["gate_progression"] = resolve_gate_progression()
            fresh["snapshot_warming"] = False
            fresh["refreshing"] = False
            fresh["snapshot_tier"] = "full"

        with _LOCK:
            _GUI_LAYERS["full"] = fresh
            _META["gui_full_ts"] = time.time()

        merged = _publish_gui_merged()
        _sync_gui_status_cache(merged)
        try:
            from runtime.unified_execution import apply_route_cache_rows

            apply_route_cache_rows(fresh.get("unified_execution_route"))
        except Exception:
            pass
        return merged
    except Exception:
        return dict(_GUI_SNAPSHOT)
    finally:
        with _LOCK:
            _META["gui_full_refreshing"] = False


def refresh_gui_snapshot() -> dict[str, Any]:
    """Backward-compatible alias — runs fast then schedules full if stale."""
    refresh_gui_fast_snapshot()
    with _LOCK:
        full_age = time.time() - float(_META.get("gui_full_ts") or 0.0)
    if full_age >= _GUI_FULL_INTERVAL_SEC:
        return refresh_gui_full_snapshot()
    return get_gui_snapshot()


def _sync_gui_status_cache(payload: dict[str, Any]) -> None:
    """Keep legacy gui_status TTL cache aligned with the readiness snapshot."""
    try:
        from api import gui_status as gs

        with gs._GUI_STATUS_LOCK:
            gs._GUI_STATUS_CACHE["ts"] = time.time()
            gs._GUI_STATUS_CACHE["data"] = copy.deepcopy(payload)
    except Exception:
        pass


def _gui_warming_skeleton() -> dict[str, Any]:
    try:
        from runtime.session_identity import build_session_identity_fields

        identity = build_session_identity_fields()
    except Exception:
        identity = {}
    prog = resolve_gate_progression()
    from api.readiness_model import build_readiness_bundle

    readiness = build_readiness_bundle(
        gate_progression=prog,
        session_status=str(identity.get("session_status") or ""),
        snapshot_warming=True,
    )
    return {
        **identity,
        **readiness,
        "gate_progression": prog,
        "snapshot_warming": True,
        "refreshing": False,
        "gui_attach_ready": False,
        "api_feed_health": {},
        "trade_pipeline_health": [],
        "pipeline_governance": {},
        "unified_execution_route": [],
        "strategy_governance": {},
        "market_rotation_status": {"rotation_state": "IDLE"},
    }


def get_health_snapshot() -> tuple[int, dict[str, Any]]:
    """O(1) health payload for /api/health — never triggers heavy rebuild."""
    snap = _HEALTH_SNAPSHOT
    if snap:
        body = dict(snap)
        age = time.time() - float(_META.get("health_ts") or 0.0)
        body["snapshot_age_s"] = round(age, 3)
        body["snapshot_stale"] = age > _HEALTH_REFRESH_INTERVAL_SEC * 3
        body = _overlay_live_iron_cage(body)
        if age > _HEALTH_REFRESH_INTERVAL_SEC * 2:
            threading.Thread(
                target=refresh_health_snapshot,
                name="readiness-health-stale-kick",
                daemon=True,
            ).start()
        body["snapshot_stale"] = age > _HEALTH_REFRESH_INTERVAL_SEC * 3
        return _http_code_for_body(body), body

    try:
        from api.health_instant import build_instant_health_response

        body = build_instant_health_response()
        body.setdefault("snapshot_age_s", 0.0)
        body.setdefault("snapshot_stale", True)
        return _http_code_for_body(body), body
    except Exception:
        body = _overlay_live_iron_cage(_compose_health_light())
        body.setdefault("snapshot_age_s", 0.0)
        body.setdefault("snapshot_stale", True)
        return _http_code_for_body(body), body


def get_gui_snapshot() -> dict[str, Any]:
    """O(1) gui_status payload — never triggers build_gui_status on request threads."""
    with _LOCK:
        if _GUI_SNAPSHOT:
            body = dict(_GUI_SNAPSHOT)
            age = time.time() - float(_META.get("gui_ts") or 0.0)
            body["snapshot_age_s"] = round(age, 3)
            body["refreshing"] = bool(_META.get("gui_refreshing"))
            return body

    body = _gui_warming_skeleton()
    with _LOCK:
        body["refreshing"] = bool(_META.get("gui_refreshing"))
    return body


def trigger_gui_refresh_async() -> None:
    """Fire-and-forget GUI snapshot rebuild when cache is cold."""
    with _LOCK:
        if _META.get("gui_full_refreshing") and _GUI_SNAPSHOT:
            return

    def _run() -> None:
        refresh_gui_fast_snapshot()
        refresh_gui_full_snapshot()

    threading.Thread(target=_run, name="gui-snapshot-kick", daemon=True).start()


def _refresher_loop() -> None:
    next_health = 0.0
    next_fast = 0.0
    next_full = 0.0
    while not _STOP.is_set():
        now = time.monotonic()
        if now >= next_health:
            try:
                refresh_health_snapshot()
            except Exception:
                pass
            next_health = now + _HEALTH_REFRESH_INTERVAL_SEC
        if now >= next_fast:
            try:
                refresh_gui_fast_snapshot()
            except Exception:
                pass
            next_fast = now + _GUI_FAST_INTERVAL_SEC
        if now >= next_full:
            try:
                refresh_gui_full_snapshot()
            except Exception:
                pass
            next_full = now + _GUI_FULL_INTERVAL_SEC
        if _STOP.wait(_LOOP_TICK_SEC):
            break


def start_readiness_snapshot_refresher() -> None:
    """Daemon thread: keep health + gui_status snapshots fresh under tick load."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_refresher_loop,
        name="readiness-snapshot-refresher",
        daemon=True,
    )
    _THREAD.start()
    threading.Thread(
        target=refresh_health_snapshot,
        name="readiness-health-kick",
        daemon=True,
    ).start()
    threading.Thread(
        target=refresh_gui_fast_snapshot,
        name="readiness-gui-fast-kick",
        daemon=True,
    ).start()
    threading.Thread(
        target=refresh_gui_full_snapshot,
        name="readiness-gui-full-kick",
        daemon=True,
    ).start()


def stop_readiness_snapshot_refresher() -> None:
    _STOP.set()


def reset_readiness_snapshot_for_tests() -> None:
    global _THREAD, _HEALTH_SNAPSHOT, _GUI_SNAPSHOT, _GUI_LAYERS
    stop_readiness_snapshot_refresher()
    with _LOCK:
        _HEALTH_SNAPSHOT = {}
        _GUI_SNAPSHOT = {}
        _GUI_LAYERS = {"fast": {}, "full": {}}
        _META.update(
            {
                "health_ts": 0.0,
                "gui_ts": 0.0,
                "gui_fast_ts": 0.0,
                "gui_full_ts": 0.0,
                "gui_refreshing": False,
                "gui_full_refreshing": False,
            }
        )
    _THREAD = None
    _STOP.clear()
    try:
        from api.endpoint_profiler import reset_profiler_for_tests

        reset_profiler_for_tests()
    except Exception:
        pass
