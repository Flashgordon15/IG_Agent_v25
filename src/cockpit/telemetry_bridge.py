"""Thread-safe telemetry bridge — trading host → Flight Deck UI (in-process queues)."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from cockpit.queue_guard import put_drop_oldest
from system.engine_log import log_engine

DEFAULT_EPICS = (
    "IX.D.NASDAQ.IFM.IP",
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.NIKKEI.IFM.IP",
)

_bridge_lock = threading.Lock()
_telemetry_queue: queue.Queue[dict[str, Any]] | None = None
_command_queue: queue.Queue[str] | None = None
_collector_thread: threading.Thread | None = None
_command_thread: threading.Thread | None = None
_stop = threading.Event()


def get_telemetry_queue() -> queue.Queue[dict[str, Any]]:
    global _telemetry_queue
    with _bridge_lock:
        if _telemetry_queue is None:
            _telemetry_queue = queue.Queue(maxsize=32)
        return _telemetry_queue


def get_command_queue() -> queue.Queue[str]:
    global _command_queue
    with _bridge_lock:
        if _command_queue is None:
            _command_queue = queue.Queue(maxsize=8)
        return _command_queue


def _target_factor() -> float:
    try:
        from intelligence.target_engine import get_target_engine

        return get_target_engine().risk_compression_factor()
    except Exception:
        return 1.0


def _target_preservation() -> bool:
    try:
        from intelligence.target_engine import get_target_engine

        return get_target_engine().capital_preservation_mode()
    except Exception:
        return False


def _target_mission_snapshot() -> dict[str, Any]:
    try:
        from intelligence.target_engine import get_target_engine

        return get_target_engine().refresh()
    except Exception:
        return {}


def _scalping_telemetry_payload(
    *,
    position_map: dict[str, dict[str, Any]],
    primary_epic: str,
    micro_confidence: float,
) -> dict[str, Any]:
    """
    Authoritative scalping block — memory-only reads on the collector thread.

    Sources: intelligence.time_decay (stall + ATR compression) and
    intelligence.velocity_filter (200ms tick burst + 90% override flag).
    """
    try:
        from intelligence.time_decay import scalping_time_decay_telemetry
        from intelligence.velocity_filter import scalping_velocity_telemetry
        from system.protective_learning import apply_test_mode_scalping_telemetry

        payload = {
            "engine_state": "ENGAGED" if position_map else "STANDBY",
            "primary_epic": str(primary_epic or "").strip(),
            "open_positions": len(position_map),
            "time_decay": scalping_time_decay_telemetry(position_map),
            "tick_velocity": scalping_velocity_telemetry(
                primary_epic,
                micro_confidence=micro_confidence,
            ),
        }
        return apply_test_mode_scalping_telemetry(payload)
    except Exception:
        return {}


def _epics_with_open_positions(position_map: dict[str, dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in position_map.values():
        if not isinstance(row, dict):
            continue
        epic = str(row.get("epic") or "").strip()
        if not epic:
            continue
        size = float(row.get("size") or row.get("dealSize") or 0)
        if size > 0 or row.get("direction") or row.get("side"):
            out.add(epic)
    return out


def _resolve_market_states_map(
    epics: tuple[str, ...],
    *,
    position_map: dict[str, dict[str, Any]],
    epic_rows: dict[str, Any],
    micro_verdicts: dict[str, Any],
) -> dict[str, str]:
    """Map each epic to LISTENING, LEARNING, or TRADING for Card B HUD badges."""
    trading_epics = _epics_with_open_positions(position_map)
    states: dict[str, str] = {}
    micro_model = None
    min_warmup = 12
    try:
        from intelligence.microstructure import MIN_WARMUP_TICKS
        from intelligence.pipeline_bridge import get_intelligence_layer

        min_warmup = MIN_WARMUP_TICKS
        micro_model = get_intelligence_layer()._worker.micro_model
    except Exception:
        pass

    for epic in epics:
        key = str(epic or "").strip()
        if not key:
            continue
        if key in trading_epics:
            states[key] = "TRADING"
            continue
        learning = False
        if micro_model is not None:
            try:
                learning = micro_model.needs_historical_warmup(key) or micro_model.tick_count(
                    key
                ) < min_warmup
            except Exception:
                learning = False
        else:
            mi = micro_verdicts.get(key) if isinstance(micro_verdicts, dict) else None
            if isinstance(mi, dict):
                conf = float(mi.get("confidence") or 0)
                regime = str(mi.get("regime") or "NEUTRAL")
                learning = conf < 25.0 and regime == "NEUTRAL"
        if learning:
            states[key] = "LEARNING"
        elif key in epic_rows:
            states[key] = "LISTENING"
        else:
            states[key] = "LISTENING"
    return states


def _resolve_global_ai_status(
    *,
    gates: dict[str, Any],
    position_map: dict[str, dict[str, Any]],
    drawdown_guard: dict[str, Any],
    position_drift: dict[str, Any],
    spread_verdicts: dict[str, Any],
    co_pilot_vitals: dict[str, Any],
) -> tuple[str, str]:
    """Return (global_ai_status_key, global_ai_status_module) for master vitals banner."""
    if drawdown_guard.get("frozen") or drawdown_guard.get("breached"):
        return "EMERGENCY", "DrawdownGuard"

    if position_drift.get("any_drift"):
        return "EMERGENCY", "PositionDrift"

    if co_pilot_vitals.get("deployment_frozen"):
        mod = str(co_pilot_vitals.get("degraded_module") or "SelfHealingSupervisor")
        return "DEGRADED", mod

    if isinstance(gates, dict):
        for gid in ("G1", "G2", "G3", "G4"):
            g = gates.get(gid)
            if isinstance(g, dict) and str(g.get("status") or "").lower() == "failed":
                return "DEGRADED", f"Gate {gid}"

    if _epics_with_open_positions(position_map):
        return "PEAK", ""

    for row in spread_verdicts.values():
        if isinstance(row, dict) and (row.get("blocked") or row.get("turbulence")):
            return "DEGRADED", "SpreadForecast"

    recent_fault = co_pilot_vitals.get("recent_fault_module")
    if recent_fault:
        return "DEGRADED", str(recent_fault)

    return "HEALTHY", ""


def _collect_snapshot(epics: tuple[str, ...]) -> dict[str, Any]:
    from system.system_state import get_system_state

    snap = get_system_state().snapshot_model().to_dict()
    gates = (snap.get("gates") or {}) if isinstance(snap, dict) else {}

    epic_rows: dict[str, Any] = {}
    raw_positions: list[dict[str, Any]] = []

    try:
        from api.agent_control import get_trading_loop

        loop_bundle = get_trading_loop()
        if loop_bundle is not None and hasattr(loop_bundle, "loops"):
            for tl in loop_bundle.loops:
                sync = getattr(tl, "_position_sync", None)
                if sync is not None and hasattr(sync, "snapshot_dict"):
                    snap = sync.snapshot_dict()
                    pmap = snap.get("position_map")
                    if isinstance(pmap, dict):
                        raw_positions.extend(pmap.values())
                    else:
                        raw_positions.extend(snap.get("positions") or [])
    except Exception:
        pass

    if not raw_positions:
        try:
            from api.agent_control import get_trading_loop

            loop = get_trading_loop()
            if loop is not None and hasattr(loop, "loops"):
                for tl in loop.loops:
                    eng = getattr(getattr(tl, "_execution_loop", None), "execution_engine", None)
                    tracker = getattr(eng, "trade_tracker", None) if eng else None
                    if tracker is None:
                        continue
                    snap_t = tracker.snapshot()
                    for row in snap_t.get("positions", []) if isinstance(snap_t, dict) else []:
                        if isinstance(row, dict):
                            raw_positions.append(dict(row))
        except Exception:
            pass

    position_map: dict[str, dict[str, Any]] = {}
    try:
        from cockpit.telemetry_schema import TelemetrySchemaMismatchError, validate_position_map

        position_map = validate_position_map(raw_positions)
    except TelemetrySchemaMismatchError as exc:
        log_engine(f"cockpit telemetry schema mismatch: {exc}")
        from trading.open_position_view import position_map_from_rows

        position_map = position_map_from_rows(raw_positions)
    except Exception:
        from trading.open_position_view import position_map_from_rows

        position_map = position_map_from_rows(raw_positions)

    spread_verdicts: dict[str, Any] = {}
    micro_verdicts: dict[str, Any] = {}
    autopilot_rating = 0.0
    session_autopilot: dict[str, Any] = {}
    micro_regime = "NEUTRAL"
    micro_conf = 0.0

    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
    except Exception:
        hub = None

    try:
        from intelligence.pipeline_bridge import get_intelligence_layer
        from intelligence.autopilot_scaling import cockpit_autopilot_rating

        layer = get_intelligence_layer()
        primary = epics[0] if epics else ""
        if primary:
            sp = layer.spread_verdict(primary)
            mi = layer.microstructure_verdict(primary)
            micro_regime = str(mi.regime)
            micro_conf = float(mi.confidence)
            autopilot_rating, session_autopilot = cockpit_autopilot_rating(
                primary,
                micro_confidence=micro_conf,
                spread_z=float(sp.z_score),
                throttle_factor=float(sp.throttle_factor),
                blocked=bool(sp.blocked),
            )
        for epic in epics:
            sp = layer.spread_verdict(epic)
            mi = layer.microstructure_verdict(epic)
            spread_verdicts[epic] = {
                "spread": sp.spread,
                "z_score": sp.z_score,
                "throttle": sp.throttle_factor,
                "blocked": sp.blocked,
                "turbulence": sp.blocked or sp.throttle_factor >= 0.5,
            }
            micro_verdicts[epic] = {
                "regime": mi.regime,
                "confidence": mi.confidence,
            }
            if hub is not None:
                q = hub.get_snapshot(epic)
                if q is not None:
                    epic_rows[epic] = {
                        "bid": q.bid,
                        "offer": q.offer,
                        "spread": q.offer - q.bid,
                        "age_s": q.age_seconds(),
                    }
    except Exception:
        pass

    trail_by_deal: dict[str, Any] = {}
    trail_rows: list[dict[str, Any]] = []
    try:
        from intelligence.alpha_trail import AlphaOptimisedTrailEngine

        micro_for_trail = {
            k: (v if isinstance(v, dict) else {"regime": getattr(v, "regime", "NEUTRAL")})
            for k, v in micro_verdicts.items()
        }
        trails = AlphaOptimisedTrailEngine().compute_for_position_map(
            position_map,
            epic_quotes=epic_rows,
            micro_verdicts=micro_for_trail,
            risk_compression_factor=_target_factor(),
            capital_preservation=_target_preservation(),
        )
        for deal_id, verdict in trails.items():
            trail_by_deal[deal_id] = verdict
            row = position_map.get(deal_id, {})
            trail_rows.append(
                {
                    "dealId": deal_id,
                    "deal_id": deal_id,
                    "epic": verdict.epic,
                    "side": verdict.side,
                    "entry": row.get("entry"),
                    "market": row.get("current"),
                    "stop": row.get("stop"),
                    "proposed_stop": verdict.proposed_stop,
                    "trail_pts": verdict.trail_distance_pts,
                    "profit_pts": verdict.profit_pts,
                    "profitAndLoss": row.get("profitAndLoss"),
                    "pnl_currency": row.get("pnl_currency"),
                }
            )
    except Exception:
        pass

    try:
        from system.env_loader import load_dotenv
        import os

        load_dotenv()
        ig_account_id = os.environ.get("IG_ACCOUNT_ID", "").strip()
    except Exception:
        ig_account_id = ""

    primary_epic = epics[0] if epics else ""
    scalping_telemetry = _scalping_telemetry_payload(
        position_map=position_map,
        primary_epic=primary_epic,
        micro_confidence=micro_conf,
    )

    position_drift: dict[str, Any] = {}
    try:
        from cockpit.position_drift import (
            build_position_drift_report,
            local_positions_from_store,
        )

        local_map = local_positions_from_store()
        position_drift = build_position_drift_report(
            broker_map=position_map,
            local_map=local_map,
        )
        for deal_id in position_drift.get("by_deal", {}):
            if deal_id in position_map:
                position_map[deal_id]["drift_detected"] = True
    except Exception:
        pass

    drawdown_guard: dict[str, Any] = {}
    try:
        from system.superjet_drawdown_guard import check_and_enforce_async

        drawdown_guard = check_and_enforce_async()
        try:
            from system.drawdown_monitor import snapshot_for_telemetry

            drawdown_guard["monitor"] = snapshot_for_telemetry()
        except Exception:
            pass
    except Exception:
        pass

    co_pilot_vitals: dict[str, Any] = {}
    try:
        from system.self_healing_supervisor import co_pilot_vitals_snapshot

        co_pilot_vitals = co_pilot_vitals_snapshot()
    except Exception:
        pass

    market_states_map = _resolve_market_states_map(
        epics,
        position_map=position_map,
        epic_rows=epic_rows,
        micro_verdicts=micro_verdicts,
    )
    global_ai_status_key, global_ai_status_module = _resolve_global_ai_status(
        gates=gates,
        position_map=position_map,
        drawdown_guard=drawdown_guard,
        position_drift=position_drift,
        spread_verdicts=spread_verdicts,
        co_pilot_vitals=co_pilot_vitals,
    )

    macro_radar: dict[str, Any] = {}
    try:
        from intelligence.macro_radar import macro_radar_telemetry

        macro_radar = macro_radar_telemetry()
    except Exception:
        pass

    shadow_trading: dict[str, Any] = {}
    try:
        from trading.shadow_executor import refresh_shadow_mtm

        shadow_trading = refresh_shadow_mtm()
    except Exception:
        pass

    order_book_imbalance: dict[str, Any] = {}
    try:
        from cockpit.telemetry_schema import OrderBookDepthPayload
        from intelligence.order_book_imbalance import compute_obi_ratio, obi_institutional_flag

        for epic_key in epics:
            q = epic_rows.get(epic_key) if isinstance(epic_rows, dict) else None
            if not isinstance(q, dict):
                continue
            bid = float(q.get("bid") or 0)
            offer = float(q.get("offer") or 0)
            if bid <= 0 or offer <= bid:
                continue
            spread = offer - bid
            payload = OrderBookDepthPayload(
                epic=epic_key,
                ts=time.time(),
                bid_levels=[{"price": bid, "size": 1.0}, {"price": bid - spread * 0.5, "size": 0.5}],
                ask_levels=[{"price": offer, "size": 1.0}, {"price": offer + spread * 0.5, "size": 0.5}],
                source="hub_proxy_l2",
            )
            ratio = compute_obi_ratio(payload)
            order_book_imbalance[epic_key] = {
                **payload.normalized_dict(),
                "institutional_flag": obi_institutional_flag(ratio),
            }
    except Exception:
        pass

    payload = {
        "ts": time.time(),
        "gates": gates,
        "boot_phase": snap.get("phase") if isinstance(snap, dict) else "",
        "epics": epic_rows,
        "spread": spread_verdicts,
        "microstructure": micro_verdicts,
        "autopilot_rating": autopilot_rating,
        "session_autopilot": session_autopilot,
        "micro_regime": micro_regime,
        "micro_confidence": micro_conf,
        "position_map": position_map,
        "positions": trail_rows or list(position_map.values()),
        "trail_by_deal": {k: v.__dict__ for k, v in trail_by_deal.items()},
        "ig_account_id": ig_account_id,
        "target_mission": _target_mission_snapshot(),
        "liquidity_wave": _liquidity_wave_snapshot(),
        "scalping_telemetry": scalping_telemetry,
        "position_drift": position_drift,
        "drawdown_guard": drawdown_guard,
        "global_ai_status_key": global_ai_status_key,
        "global_ai_status_module": global_ai_status_module,
        "market_states_map": market_states_map,
        "co_pilot_vitals": co_pilot_vitals,
        "macro_radar": macro_radar,
        "shadow_trading": shadow_trading,
        "order_book_imbalance": order_book_imbalance,
    }
    try:
        from cockpit.avionics_markets import package_avionics_hud_broadcast

        payload = package_avionics_hud_broadcast(payload)
    except Exception as exc:
        log_engine(
            f"cockpit telemetry avionics package failed: {type(exc).__name__}: {exc}"
        )
    return payload


def _liquidity_wave_snapshot() -> dict[str, Any]:
    try:
        from intelligence.liquidity_wave import liquidity_wave_snapshot

        return liquidity_wave_snapshot()
    except Exception:
        return {}


def _collector_loop(epics: tuple[str, ...], hz: float) -> None:
    interval = max(0.1, 1.0 / max(0.5, float(hz)))
    q = get_telemetry_queue()
    while not _stop.is_set():
        try:
            payload = _collect_snapshot(epics)
            put_drop_oldest(q, payload)
        except Exception as e:
            log_engine(f"cockpit telemetry collect failed: {type(e).__name__}: {e}")
        _stop.wait(interval)


def _command_loop() -> None:
    cmd_q = get_command_queue()
    while not _stop.is_set():
        try:
            cmd = cmd_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if cmd == "EMERGENCY_FLATTEN":
            try:
                from cockpit.emergency import execute_emergency_cockpit_override

                execute_emergency_cockpit_override()
            except Exception as e:
                log_engine(f"cockpit emergency failed: {type(e).__name__}: {e}")


def start_telemetry_bridge(
    *,
    epics: tuple[str, ...] | None = None,
    hz: float = 2.5,
) -> None:
    global _collector_thread, _command_thread
    _stop.clear()
    target_epics = epics or DEFAULT_EPICS
    if _collector_thread is None or not _collector_thread.is_alive():
        _collector_thread = threading.Thread(
            target=_collector_loop,
            args=(target_epics, hz),
            daemon=True,
            name="CockpitTelemetryCollector",
        )
        _collector_thread.start()
    if _command_thread is None or not _command_thread.is_alive():
        _command_thread = threading.Thread(
            target=_command_loop,
            daemon=True,
            name="CockpitCommandWorker",
        )
        _command_thread.start()


def bridge_is_active() -> bool:
    """True when the telemetry collector thread is running."""
    return _collector_thread is not None and _collector_thread.is_alive()


def stop_telemetry_bridge() -> None:
    _stop.set()


def reset_telemetry_bridge_for_tests() -> None:
    global _telemetry_queue, _command_queue, _collector_thread, _command_thread
    stop_telemetry_bridge()
    with _bridge_lock:
        _telemetry_queue = None
        _command_queue = None
        _collector_thread = None
        _command_thread = None
    _stop.clear()
