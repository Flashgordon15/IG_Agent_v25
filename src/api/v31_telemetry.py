"""v31 dashboard telemetry — hub quotes, live positions, closed trade history."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analytics.triage_db import connect_triage_sqlite
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub
from system.system_state import GateStatus, get_system_state


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _triage_v31_path() -> Path:
    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db").resolve()


def _resolve_position_sync() -> Any | None:
    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            return sync
    except Exception:
        pass
    try:
        from api.agent_control import get_trading_loop

        orch = get_trading_loop()
        for loop in list(getattr(orch, "loops", []) or []) if orch else []:
            sync = getattr(loop, "_position_sync", None)
            if sync is not None:
                return sync
    except Exception:
        pass
    return None


def _gate_status(gates: dict[str, Any], gate_id: str) -> str:
    raw = gates.get(gate_id) or {}
    if hasattr(raw, "status"):
        return str(raw.status).lower()
    return str(raw.get("status") or GateStatus.PENDING).lower()


def resolve_active_boot_gate() -> int | None:
    """Return 1–5 for the active boot gate, or None when operational."""
    snap = get_system_state().snapshot()
    phase = str(snap.get("phase") or "").upper()
    gates = snap.get("gates") or {}

    if phase == "FAILED" or any(
        _gate_status(gates, gid) == str(GateStatus.FAILED) for gid in ("G1", "G2", "G3", "G4", "G5")
    ):
        for gid, num in (("G5", 5), ("G4", 4), ("G3", 3), ("G2", 2), ("G1", 1)):
            if _gate_status(gates, gid) == str(GateStatus.FAILED):
                return num
        return 1

    if phase == "READY":
        return None

    for gid, num in (("G5", 5), ("G4", 4), ("G3", 3), ("G2", 2), ("G1", 1)):
        if _gate_status(gates, gid) != str(GateStatus.COMPLETE):
            return num
    return None


def resolve_pacing_interval_sec() -> float:
    """Current REST/share pacing interval in seconds."""
    try:
        from system.share_pacing import SharePacingController

        ctrl = SharePacingController()
        if ctrl.enabled():
            return float(ctrl.pacing_interval_sec())
    except Exception:
        pass
    sync = _resolve_position_sync()
    if sync is not None:
        try:
            fn = getattr(sync, "_effective_interval", None)
            if callable(fn):
                return float(fn())
            return float(getattr(sync, "_interval", 5.0))
        except Exception:
            pass
    return 5.0


def resolve_block_reason() -> str:
    """Aggregate active entry-suppression reasons from risk / capital guards."""
    reasons: list[str] = []

    try:
        from runtime.strategy_kill_switch import is_strategy_kill_active

        if is_strategy_kill_active():
            reasons.append("BROKER_STATE_MISMATCH")
    except Exception:
        pass

    try:
        from system.qmm_process_supervisor import process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked and detail:
            reasons.append(detail)
    except Exception:
        pass

    try:
        from api.agent_control import is_paused

        if is_paused():
            reasons.append("api_trading_paused")
    except Exception:
        pass

    try:
        from system.rest_api_budget import get_rest_api_budget

        budget = get_rest_api_budget()
        if budget._preemptive_pause_active():
            reasons.append("rest_budget_preemptive_pause")
    except Exception:
        pass

    try:
        from api.snapshot_store import get_tick

        tick = get_tick()
        if isinstance(tick, dict):
            br = str(tick.get("block_reason") or "").strip()
            if br:
                reasons.append(br)
            sig = tick.get("signal")
            if isinstance(sig, dict):
                sbr = str(sig.get("block_reason") or "").strip()
                if sbr:
                    reasons.append(sbr)
    except Exception:
        pass

    if not reasons:
        return ""
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return "; ".join(ordered)


def resolve_last_gate_suppression_reason() -> str:
    """Most recent micro-scalper / gate hold-back reason (falls back to block_reason)."""
    try:
        from runtime.dual_core_execution import get_last_gate_suppression_reason

        detail = str(get_last_gate_suppression_reason() or "").strip()
        if detail:
            return detail
    except Exception:
        pass
    try:
        from system.demo_execution_trace import get_demo_diagnostics_snapshot

        diag = get_demo_diagnostics_snapshot()
        rej = str(getattr(diag, "last_rejection", "") or "").strip()
        if rej:
            return rej
    except Exception:
        pass
    return resolve_block_reason()


def resolve_ml_current_alpha_weight() -> float | None:
    """Active SGD regressor strictness factor (sigmoid-scored at warmed feature means)."""
    try:
        import math

        from system.ml.cold_start_compiler import load_warmed_alpha_manifest
        from system.ml.twin_engine_core import get_twin_engine_core

        live = get_twin_engine_core().live.weights_snapshot()
        manifest = load_warmed_alpha_manifest() or {}
        stats = manifest.get("feature_stats") or {}
        features: dict[str, float] = {}
        for key in live.coeffs:
            row = stats.get(key) if isinstance(stats.get(key), dict) else {}
            features[key] = float((row or {}).get("mean") or 0.0)
        raw = float(live.score(features))
        return round(1.0 / (1.0 + math.exp(-raw)), 4)
    except Exception:
        return None


def resolve_live_15min_macro_trend() -> str:
    try:
        from runtime.dual_core_execution import resolve_live_15min_macro_trend as _trend

        return str(_trend() or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def resolve_ml_alpha_weight() -> float | None:
    """Alias — active SGD regressor strictness factor."""
    return resolve_ml_current_alpha_weight()


def resolve_ml_radar_axes() -> dict[str, float]:
    """Normalized ML parameter axes for cockpit radar plot."""
    axes: dict[str, float] = {"alpha_weight": 0.5}
    alpha = resolve_ml_alpha_weight()
    if alpha is not None:
        axes["alpha_weight"] = float(alpha)
    try:
        from system.ml.twin_engine_core import get_twin_engine_core

        live = get_twin_engine_core().live.weights_snapshot()
        for key, val in live.coeffs.items():
            axes[str(key)] = round(min(1.0, abs(float(val)) / 2.0), 4)
    except Exception:
        pass
    return axes


def resolve_ticks_processed() -> int:
    """Total data-matrix velocity count (ring buffer + fulfillment cache)."""
    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload

        payload = get_fulfillment_payload()
        dv = payload.get("data_velocity") or {}
        ticks = int(dv.get("ticks_cached") or payload.get("ticks_cached") or 0)
        if ticks > 0:
            return ticks
    except Exception:
        pass
    try:
        from system.ipc.ring_buffer import read_cockpit_shm

        snap = read_cockpit_shm()
        if isinstance(snap, dict):
            return int(snap.get("ticks_cached") or 0)
    except Exception:
        pass
    return 0


def _trail_metrics_for_position(pos: dict[str, Any], rest_client: Any | None) -> tuple[float, float]:
    """Return (trail_progress_pct, points_to_trail) for step-trailing engine."""
    from runtime.dual_core_execution import MICRO_TP_POINTS
    from system.pnl_math import ig_points_to_price_delta

    entry = float(pos.get("entry") or pos.get("level") or 0)
    current = float(pos.get("current") or pos.get("broker_mark") or entry)
    direction = str(pos.get("direction") or "BUY").upper()
    epic = str(pos.get("epic") or "")
    if entry <= 0 or not epic:
        return 0.0, float(MICRO_TP_POINTS)

    step_pts = float(MICRO_TP_POINTS)
    try:
        if rest_client is not None:
            from execution.live_broker_order_router import resolve_min_stop_distance_points

            step_pts = max(step_pts, float(resolve_min_stop_distance_points(rest_client, epic)))
    except Exception:
        pass

    delta = abs(float(ig_points_to_price_delta(epic, 1.0)) or 1e-9)
    if direction == "BUY":
        favorable_pts = max(0.0, (current - entry) / delta)
    else:
        favorable_pts = max(0.0, (entry - current) / delta)

    points_to_trail = round(max(0.0, step_pts - favorable_pts), 3)
    trail_progress_pct = round(min(100.0, max(0.0, (favorable_pts / step_pts) * 100.0)), 2)
    return trail_progress_pct, points_to_trail


def build_active_positions_array() -> list[dict[str, Any]]:
    """Open positions enriched with step-trail progress metrics."""
    sync = _resolve_position_sync()
    rows: list[dict[str, Any]] = []
    rest_client = None

    if sync is None:
        return rows

    snap_fn = getattr(sync, "snapshot_dict", None)
    if not callable(snap_fn):
        return rows

    raw = snap_fn()
    position_map = dict(raw.get("position_map") or {})
    for deal_id, pos in position_map.items():
        if not isinstance(pos, dict):
            continue
        trail_pct, points_to_trail = _trail_metrics_for_position(pos, rest_client)
        rows.append(
            {
                "timestamp": raw.get("last_sync_at") or _utc_now_iso(),
                "opened_at": pos.get("opened_at"),
                "epic": pos.get("epic"),
                "direction": pos.get("direction"),
                "entry": pos.get("entry") or pos.get("level"),
                "dealId": deal_id or pos.get("dealId") or pos.get("deal_id"),
                "deal_id": deal_id or pos.get("deal_id"),
                "size": pos.get("size"),
                "pnl": pos.get("pnl_gbp") or pos.get("upl") or pos.get("profitAndLoss"),
                "pnl_gbp": pos.get("pnl_gbp"),
                "current": pos.get("current"),
                "stop": pos.get("stop") or pos.get("stop_level"),
                "trail_progress_pct": trail_pct,
                "points_to_trail": points_to_trail,
            }
        )
    return rows


_rtt_probe_cache: dict[str, Any] = {"ts": 0.0, "rtt_ms": None}


def resolve_account_capital_fields() -> dict[str, Any]:
    """Live IG Demo account balance + available margin from GET /accounts."""
    out: dict[str, Any] = {
        "account_balance_gbp": None,
        "account_available_gbp": None,
        "account_profit_loss_gbp": None,
    }
    try:
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import get_shared_rest_client

        cred = try_load_credentials()
        if not cred.ok or cred.credentials is None:
            return out
        rest = get_shared_rest_client(cred.credentials)
        summary = rest.get_cached_account_summary()
        if not any(v is not None for v in summary.values()):
            summary = rest.maybe_refresh_account_summary(min_interval=30.0)
        out["account_balance_gbp"] = summary.get("balance")
        out["account_available_gbp"] = summary.get("available")
        out["account_profit_loss_gbp"] = summary.get("profit_loss")
    except Exception:
        pass
    return out


def _probe_broker_network_rtt_ms(*, min_interval_sec: float = 15.0) -> float | None:
    """Round-trip ping to demo-api.ig.com gateway (throttled)."""
    global _rtt_probe_cache
    now = time.time()
    cached = _rtt_probe_cache.get("rtt_ms")
    if cached is not None and now - float(_rtt_probe_cache.get("ts") or 0.0) < min_interval_sec:
        return float(cached)
    try:
        import httpx

        t0 = time.perf_counter()
        httpx.get(
            "https://demo-api.ig.com/gateway/deal",
            timeout=1.5,
            follow_redirects=False,
        )
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
        _rtt_probe_cache = {"ts": now, "rtt_ms": ms}
        return ms
    except Exception:
        return float(cached) if cached is not None else None


def resolve_transport_telemetry_fields(*, loop_start: float | None = None) -> dict[str, Any]:
    """Real-world broker RTT + local telemetry thread processing delay."""
    t0 = loop_start if loop_start is not None else time.perf_counter()
    rtt = _probe_broker_network_rtt_ms()
    local_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {
        "broker_network_rtt_ms": rtt,
        "local_thread_latency_ms": local_ms,
    }


def resolve_high_speed_telemetry_fields() -> dict[str, Any]:
    from runtime.dual_core_execution import (
        MACRO_Z_THRESHOLD,
        get_dual_core_snapshot,
        get_effective_micro_z_threshold,
        get_execution_focus_state,
        get_stacked_asset_channels,
        get_z_score_stream,
        resolve_core_b_gate_stack,
    )

    snap = get_dual_core_snapshot()
    alpha = resolve_ml_alpha_weight()
    gate_stack = resolve_core_b_gate_stack()
    focus = get_execution_focus_state()
    stacked = focus.get("stacked_asset_channels") or get_stacked_asset_channels()
    return {
        "live_calculated_zscore": round(float(snap.live_calculated_zscore), 4),
        "ml_alpha_weight": alpha,
        "ticks_processed": resolve_ticks_processed(),
        "active_positions": build_active_positions_array(),
        "z_score_stream": get_z_score_stream(),
        "stacked_dual_asset_mode": True,
        "stacked_asset_channels": stacked,
        "strategy_thresholds": {
            "macro_breakout_z": MACRO_Z_THRESHOLD,
            "micro_scalp_z": 0.50,
            "micro_arm_z": get_effective_micro_z_threshold(),
        },
        "ml_radar_axes": resolve_ml_radar_axes(),
        "gate_stack_matrix": gate_stack,
        "core_b_satellite_uncoupled": gate_stack.get("core_b_satellite_uncoupled"),
        **focus,
        "focus_z_score_stream": focus.get("focus_z_score_stream") or get_z_score_stream(),
    }


def resolve_cognitive_router_fields() -> dict[str, Any]:
    alpha = resolve_ml_alpha_weight()
    return {
        "live_15min_macro_trend": resolve_live_15min_macro_trend(),
        "ml_current_alpha_weight": alpha,
        "ml_alpha_weight": alpha,
        "last_gate_suppression_reason": resolve_last_gate_suppression_reason(),
    }


def resolve_risk_tracking_fields() -> dict[str, Any]:
    gate = resolve_active_boot_gate()
    return {
        "boot_gate": gate,
        "pacing_interval_sec": round(resolve_pacing_interval_sec(), 3),
        "block_reason": resolve_block_reason(),
    }


def build_v31_telemetry() -> dict[str, Any]:
    """Core asset quote map from MarketDataHub (non-blocking read)."""
    t_build = time.perf_counter()
    hub = get_market_data_hub()
    assets: dict[str, Any] = {}
    for epic in NIGHT_MATRIX_EPICS:
        snap = hub.get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            assets[epic] = {
                "epic": epic,
                "bid": None,
                "offer": None,
                "mid": None,
                "spread": None,
                "age_s": None,
                "source": None,
                "fresh": False,
            }
            continue
        mid = (float(snap.bid) + float(snap.offer)) / 2.0
        assets[epic] = {
            "epic": epic,
            "bid": float(snap.bid),
            "offer": float(snap.offer),
            "mid": round(mid, 5),
            "spread": round(float(snap.offer) - float(snap.bid), 5),
            "age_s": round(snap.age_seconds(), 2),
            "source": str(snap.source or ""),
            "fresh": snap.age_seconds() <= 45.0,
        }
    fresh = sum(1 for a in assets.values() if a.get("fresh"))
    from runtime.dual_core_execution import dual_core_status_dict

    dual = dual_core_status_dict()
    cognitive = resolve_cognitive_router_fields()
    high_speed = resolve_high_speed_telemetry_fields()
    account = resolve_account_capital_fields()
    transport = resolve_transport_telemetry_fields(loop_start=t_build)
    return {
        "ok": True,
        "ts": _utc_now_iso(),
        "assets": assets,
        "asset_count": len(assets),
        "fresh_count": fresh,
        "execution_mode": dual.get("execution_mode"),
        "volatility_z_score": dual.get("volatility_z_score"),
        "dual_core_status": dual,
        **cognitive,
        **high_speed,
        **account,
        **transport,
    }


def build_v31_positions() -> dict[str, Any]:
    """Live open contracts from IgPositionSync snapshot."""
    sync = _resolve_position_sync()
    if sync is None:
        return {
            "ok": True,
            "ts": _utc_now_iso(),
            "total_open": 0,
            "positions": {},
            "sync_status": "unavailable",
        }
    snap_fn = getattr(sync, "snapshot_dict", None)
    if not callable(snap_fn):
        return {
            "ok": True,
            "ts": _utc_now_iso(),
            "total_open": 0,
            "positions": {},
            "sync_status": "no_snapshot",
        }
    raw = snap_fn()
    position_map = dict(raw.get("position_map") or {})
    return {
        "ok": True,
        "ts": _utc_now_iso(),
        "total_open": int(raw.get("total_open") or 0),
        "account_upl": float(raw.get("account_upl") or 0.0),
        "sync_status": str(raw.get("sync_status") or ""),
        "last_sync_at": raw.get("last_sync_at"),
        "positions": position_map,
    }


def build_v31_history(*, limit: int = 10) -> dict[str, Any]:
    """Latest closed trade outcomes from triage_v31.db."""
    lim = max(1, min(int(limit), 50))
    path = _triage_v31_path()
    if not path.is_file():
        return {"ok": True, "ts": _utc_now_iso(), "rows": [], "count": 0}

    conn = connect_triage_sqlite(path)
    try:
        cur = conn.execute(
            """
            SELECT ticket, asset, epic, direction, size, entry_price, exit_price,
                   gross_pnl, net_pnl, exit_timestamp, result
            FROM closed_positions
            ORDER BY exit_timestamp DESC
            LIMIT ?
            """,
            (lim,),
        )
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            ticket, asset, epic, direction, size, entry, exit_px, gross, net, exited, result = row
            rows.append(
                {
                    "ticket": ticket,
                    "asset": asset,
                    "epic": epic or asset,
                    "direction": direction,
                    "action": direction,
                    "size": float(size or 0),
                    "entry": float(entry or 0),
                    "exit": float(exit_px or 0),
                    "gross_pnl": float(gross or 0),
                    "pnl_gbp": float(net or 0),
                    "net_pnl": float(net or 0),
                    "closed_at": exited,
                    "executed_at": exited,
                    "result": str(result or "").upper(),
                    "status": "CLOSED",
                }
            )
    except Exception as exc:
        return {
            "ok": False,
            "ts": _utc_now_iso(),
            "rows": [],
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        conn.close()

    return {"ok": True, "ts": _utc_now_iso(), "rows": rows, "count": len(rows)}
