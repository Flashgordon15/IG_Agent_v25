"""Live internal agent state — updated on loop ticks, served via /api/state and /ws/state.

Telemetry only: no trading decisions. HTTP/WebSocket handlers read pre-built snapshots.
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from api.readiness_snapshot import get_gui_snapshot, resolve_gate_progression
from api.snapshot_store import get_tick, subscribe as subscribe_tick

_STATE: dict[str, Any] = {
    "version": 0,
    "updated_at": None,
    "gate_progression": {},
    "feeds": [],
    "routing": [],
    "risk_envelope": [],
    "governance_flags": [],
    "positions": [],
    "pipeline": [],
    "runtime": {},
    "legacy": {},
}
# Pre-built on write path — HTTP/WS readers shallow-copy only (no deepcopy on hot path).
_PUBLIC_SNAPSHOT: dict[str, Any] = {}
_LOCK = threading.RLock()
_SUBSCRIBERS: list[Callable[[dict[str, Any]], None]] = []
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_ADVISORY_INTERVAL_SEC = 2.0
_TICK_SUB_UNSUB: Callable[[], None] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _legacy_from_tick(tick: dict[str, Any]) -> dict[str, Any]:
    sig = tick.get("signal") or {}
    pts = tick.get("points") or {}
    return {
        "bid": tick.get("bid"),
        "offer": tick.get("offer"),
        "agent_state": pts.get("state", "CAUTION"),
        "points_trade": float(pts.get("last_trade") or 0),
        "points_session": float(pts.get("session") or 0),
        "points_cumulative": float(pts.get("cumulative") or 0),
        "ml_confidence": float(sig.get("confidence") or 0),
        "signal_strength": float(sig.get("confidence") or 0),
        "fitness_score": float(sig.get("fitness") or 0),
        "fitness_factors": sig.get("fitness_factors") or {},
        "signal_threshold": float(sig.get("threshold") or 0),
        "config_signal_threshold": float(sig.get("config_signal_threshold") or 0),
        "min_size_threshold": float(sig.get("min_size_threshold") or 0),
        "points_confidence_floor": float(sig.get("points_confidence_floor") or 0),
        "regime": tick.get("regime"),
        "win_rate_today": tick.get("win_rate_today"),
        "win_rate_alltime": tick.get("win_rate_20"),
        "daily_pnl_gbp": float(tick.get("daily_pnl_gbp") or 0),
        "stream_status": tick.get("stream_status", "DISCONNECTED"),
        "rest_budget": tick.get("rest_calls_min", 0),
        "spread_current": tick.get("spread"),
        "spread_normal": tick.get("spread_normal"),
        "sentiment_factor": tick.get("sentiment_factor"),
    }


def _feeds_from_hub(*, epic: str | None = None, bid: float | None = None, offer: float | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

        hub = get_market_data_hub()
        for matrix_epic in NIGHT_MATRIX_EPICS:
            snap = hub.get_snapshot(matrix_epic)
            age_s: float | None = None
            row_bid: float | None = None
            row_offer: float | None = None
            if snap is not None:
                age_s = float(snap.age_seconds())
                row_bid = float(snap.bid)
                row_offer = float(snap.offer)
            if matrix_epic == epic and bid is not None and offer is not None:
                row_bid = float(bid)
                row_offer = float(offer)
                age_s = 0.0
            rows.append(
                {
                    "epic": matrix_epic,
                    "fresh": age_s is not None and age_s <= 45.0,
                    "age_s": age_s,
                    "bid": row_bid,
                    "offer": row_offer,
                    "latency_ms": round(age_s * 1000.0, 1) if age_s is not None else None,
                }
            )
    except Exception:
        if epic:
            rows.append(
                {
                    "epic": epic,
                    "fresh": bid is not None and offer is not None,
                    "age_s": 0.0 if bid is not None else None,
                    "bid": bid,
                    "offer": offer,
                    "latency_ms": 0.0 if bid is not None else None,
                }
            )
    return rows


def _governance_flags_from_gui(gui: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for row in gui.get("hard_enforcement_decisions") or []:
        if not isinstance(row, dict) or not row.get("active"):
            continue
        flags.append(
            {
                "type": "hard_enforcement",
                "epic": row.get("epic"),
                "active": True,
                "reason": row.get("reason"),
            }
        )
    gov = gui.get("pipeline_governance") or {}
    if isinstance(gov, dict):
        posture = gov.get("risk_posture")
        if posture:
            flags.append({"type": "pipeline_governance", "active": True, "reason": str(posture)})
    session_gov = gui.get("session_governance") or {}
    if isinstance(session_gov, dict):
        for key in ("stand_down", "session_cap_reached", "cooldown_active"):
            if session_gov.get(key):
                flags.append(
                    {
                        "type": "session_governance",
                        "active": True,
                        "reason": key,
                    }
                )
    return flags


def _routing_rows() -> list[dict[str, Any]]:
    try:
        from runtime.unified_execution import cached_unified_routes

        cached = cached_unified_routes()
        if cached:
            return cached
    except Exception:
        pass
    gui = get_gui_snapshot()
    routes = gui.get("unified_execution_route")
    return list(routes) if isinstance(routes, list) else []


def _merge_advisory_fields() -> None:
    tick = get_tick()
    legacy = _legacy_from_tick(tick)
    positions = list(tick.get("positions") or [])
    gui = get_gui_snapshot()
    warming = bool(gui.get("snapshot_warming"))

    routing = _routing_rows()
    risk_envelope = gui.get("regime_risk_envelope") if not warming else []
    pipeline = gui.get("trade_pipeline_health") if not warming else []
    feeds = _feeds_from_hub()
    if not warming:
        gui_feeds = gui.get("api_feed_health")
        if isinstance(gui_feeds, list) and gui_feeds:
            feeds = list(gui_feeds)
        elif isinstance(gui_feeds, dict) and gui_feeds:
            feeds = list(gui_feeds.values())

    governance_flags = _governance_flags_from_gui(gui) if not warming else []

    from api.readiness_model import build_readiness_bundle

    readiness = build_readiness_bundle(
        gate_progression=resolve_gate_progression(),
        api_feed_health=feeds,
        unified_execution_route=routing,
        regime_risk_envelope=risk_envelope if isinstance(risk_envelope, list) else [],
        pipeline_governance=(gui.get("pipeline_governance") or {}) if not warming else {},
        session_governance=(gui.get("session_governance") or {}) if not warming else {},
        hard_enforcement_decisions=gui.get("hard_enforcement_decisions") if not warming else [],
        session_status=str(gui.get("session_status") or ""),
        snapshot_warming=warming,
    )

    with _LOCK:
        _STATE["legacy"] = legacy
        _STATE["positions"] = positions
        _STATE["routing"] = routing
        _STATE["risk_envelope"] = risk_envelope if isinstance(risk_envelope, list) else []
        _STATE["pipeline"] = pipeline if isinstance(pipeline, list) else []
        _STATE["feeds"] = feeds
        _STATE["governance_flags"] = governance_flags
        _STATE.update(readiness)
        if not warming:
            _STATE["session_id"] = gui.get("session_id")
            _STATE["session_status"] = gui.get("session_status")
            _STATE["account_scope"] = gui.get("account_scope")


def record_loop_tick(
    *,
    epic: str,
    bid: float | None = None,
    offer: float | None = None,
    latency_ms: float | None = None,
) -> None:
    """Hot-path tick hook — fast in-memory merge only."""
    try:
        from api.agent_health import get_runtime_tick_fields

        runtime = get_runtime_tick_fields()
    except Exception:
        runtime = {}

    feeds = _feeds_from_hub(epic=epic, bid=bid, offer=offer)
    gate = resolve_gate_progression()

    with _LOCK:
        _STATE["version"] = int(_STATE.get("version") or 0) + 1
        _STATE["updated_at"] = _utc_now()
        _STATE["last_tick_epic"] = epic
        _STATE["last_tick_latency_ms"] = latency_ms
        _STATE["feeds"] = feeds
        _STATE["gate_progression"] = gate
        _STATE["runtime"] = runtime
        try:
            from api.readiness_model import build_readiness_bundle

            _STATE.update(
                build_readiness_bundle(
                    gate_progression=gate,
                    api_feed_health=feeds,
                    unified_execution_route=_STATE.get("routing"),
                    regime_risk_envelope=_STATE.get("risk_envelope"),
                    hard_enforcement_decisions=[
                        f
                        for f in (_STATE.get("governance_flags") or [])
                        if isinstance(f, dict) and f.get("type") == "hard_enforcement"
                    ],
                )
            )
        except Exception:
            pass
        snapshot = _public_view_locked()

    _notify_subscribers(snapshot)


def _on_dashboard_tick(tick: dict[str, Any]) -> None:
    """Lightweight merge when dashboard snapshot publishes (all-epic positions)."""
    with _LOCK:
        _STATE["version"] = int(_STATE.get("version") or 0) + 1
        _STATE["updated_at"] = _utc_now()
        _STATE["legacy"] = _legacy_from_tick(tick)
        _STATE["positions"] = list(tick.get("positions") or [])
        snapshot = _public_view_locked()
    _notify_subscribers(snapshot)


def _public_view_locked() -> dict[str, Any]:
    global _PUBLIC_SNAPSHOT
    _PUBLIC_SNAPSHOT = copy.deepcopy(_STATE)
    _PUBLIC_SNAPSHOT["snapshot_age_s"] = 0.0
    return _PUBLIC_SNAPSHOT


def get_agent_state() -> dict[str, Any]:
    """O(1) snapshot for /api/state — never blocks on advisory rebuild."""
    with _LOCK:
        body = _PUBLIC_SNAPSHOT.copy() if _PUBLIC_SNAPSHOT else copy.deepcopy(_STATE)
    ts = body.get("updated_at")
    if ts:
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            body["snapshot_age_s"] = round(
                max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds()),
                3,
            )
        except Exception:
            body["snapshot_age_s"] = None
    else:
        body["snapshot_age_s"] = None
    return body


def get_api_state_response() -> dict[str, Any]:
    """Flat legacy keys + structured live-state bundle for cockpit."""
    state = get_agent_state()
    legacy = dict(state.get("legacy") or {})
    try:
        from system.paths import logs_dir

        legacy["watchdog_failed"] = (logs_dir() / "watchdog_failed.txt").exists()
    except Exception:
        legacy["watchdog_failed"] = False
    legacy.update(
        {
            "version": state.get("version"),
            "updated_at": state.get("updated_at"),
            "gate_progression": state.get("gate_progression") or {},
            "feeds": state.get("feeds") or [],
            "routing": state.get("routing") or [],
            "risk_envelope": state.get("risk_envelope") or [],
            "governance_flags": state.get("governance_flags") or [],
            "positions": state.get("positions") or [],
            "pipeline": state.get("pipeline") or [],
            "runtime": state.get("runtime") or {},
            "snapshot_age_s": state.get("snapshot_age_s"),
            "session_id": state.get("session_id"),
            "session_status": state.get("session_status"),
            "account_scope": state.get("account_scope"),
            "readiness_level": state.get("readiness_level"),
            "readiness_label": state.get("readiness_label"),
            "subsystem_readiness": state.get("subsystem_readiness"),
            "cockpit_usable": state.get("cockpit_usable"),
            "partial_ready": state.get("partial_ready"),
            "trading_ready": state.get("trading_ready"),
        }
    )
    return legacy


def subscribe_state(callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    with _LOCK:
        _SUBSCRIBERS.append(callback)

    def _unsub() -> None:
        with _LOCK:
            if callback in _SUBSCRIBERS:
                _SUBSCRIBERS.remove(callback)

    return _unsub


def _notify_subscribers(snapshot: dict[str, Any]) -> None:
    with _LOCK:
        subscribers = list(_SUBSCRIBERS)
    for cb in subscribers:
        try:
            cb(snapshot)
        except Exception:
            pass


def _refresher_loop() -> None:
    while not _STOP.is_set():
        try:
            _merge_advisory_fields()
            with _LOCK:
                _STATE["version"] = int(_STATE.get("version") or 0) + 1
                _STATE["updated_at"] = _utc_now()
                _STATE["gate_progression"] = resolve_gate_progression()
                snapshot = _public_view_locked()
            _notify_subscribers(snapshot)
        except Exception:
            pass
        if _STOP.wait(_ADVISORY_INTERVAL_SEC):
            break


def start_agent_state_service() -> None:
    """Background advisory merge + dashboard tick subscription."""
    global _THREAD, _TICK_SUB_UNSUB
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()

    if _TICK_SUB_UNSUB is None:
        _TICK_SUB_UNSUB = subscribe_tick(_on_dashboard_tick)

    _THREAD = threading.Thread(
        target=_refresher_loop,
        name="agent-state-refresher",
        daemon=True,
    )
    _THREAD.start()


def stop_agent_state_service() -> None:
    global _TICK_SUB_UNSUB
    _STOP.set()
    if _TICK_SUB_UNSUB is not None:
        try:
            _TICK_SUB_UNSUB()
        except Exception:
            pass
        _TICK_SUB_UNSUB = None


def reset_agent_state_for_tests() -> None:
    global _THREAD, _PUBLIC_SNAPSHOT
    stop_agent_state_service()
    with _LOCK:
        _STATE.clear()
        _STATE.update(
            {
                "version": 0,
                "updated_at": None,
                "gate_progression": {},
                "feeds": [],
                "routing": [],
                "risk_envelope": [],
                "governance_flags": [],
                "positions": [],
                "pipeline": [],
                "runtime": {},
                "legacy": {},
            }
        )
        _PUBLIC_SNAPSHOT = {}
        _SUBSCRIBERS.clear()
    _THREAD = None
    _STOP.clear()
