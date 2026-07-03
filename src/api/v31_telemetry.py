"""v31 dashboard telemetry — hub quotes, live positions, closed trade history."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analytics.triage_db import connect_triage_sqlite, connect_triage_sqlite_readonly
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


_COCKPIT_BLOCK_TOKEN = "COCKPIT_EMERGENCY_OVERRIDE"


def _purge_cockpit_block_token() -> None:
    """Drop stale cockpit emergency latch from all hot-path memory."""
    try:
        from cockpit.emergency import purge_cockpit_emergency_persistence

        purge_cockpit_emergency_persistence()
    except Exception:
        pass


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
        from system.config_loader import get_config
        from system.demo_execution_plane import demo_throughput_active
        from system.rest_api_budget import get_rest_api_budget

        if not demo_throughput_active(get_config()):
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

    if any(_COCKPIT_BLOCK_TOKEN in str(r) for r in reasons):
        _purge_cockpit_block_token()
        reasons = [r for r in reasons if _COCKPIT_BLOCK_TOKEN not in str(r)]

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
    """Active SGD regressor strictness factor — sovereignty worker takes precedence."""
    try:
        from trading.continuous_optimization_worker import get_continuous_optimization_worker
        from runtime.dual_core_execution import is_forex_failover_active

        worker = get_continuous_optimization_worker()
        if is_forex_failover_active() or worker.is_sovereignty_active():
            return worker.get_ml_alpha_weight()
    except Exception:
        pass
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
    """Total data-matrix velocity count — hub SHM first, fallback to rotation sweep count."""
    try:
        from system.ipc.ring_buffer import read_cockpit_shm

        snap = read_cockpit_shm()
        if isinstance(snap, dict):
            v = int(snap.get("ticks_cached") or 0)
            if v > 0:
                return v
    except Exception:
        pass
    # Fallback: use dual_core rotation_sweep_count (proxy for total processed sweeps)
    try:
        from runtime.dual_core_execution import get_rotation_state, _ticks_per_minute, ROTATION_UNIVERSE

        state = get_rotation_state()
        sweep = int(state.get("rotation_sweep_count") or 0)
        if sweep > 0:
            return sweep
        # Last resort: sum tpm across universe
        tpm_sum = sum(_ticks_per_minute(epic) for epic in ROTATION_UNIVERSE)
        if tpm_sum > 0:
            return tpm_sum
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
    """Live ledger — SQL first; fall back to one-time IG history hydration cache."""
    rows = _query_live_production_ledger_sql()
    if rows:
        return _enrich_positions_spot_pnl(rows, None)
    try:
        from runtime.ledger_hydration_core import get_cached_ledger_rows, ledger_cache_ready

        if ledger_cache_ready():
            return get_cached_ledger_rows()
    except Exception:
        pass
    return []


def _query_live_production_ledger_sql(*, limit: int = 50) -> list[dict[str, Any]]:
    """
    Authoritative ledger from disk on every telemetry poll.

    Primary: production_orders (ACCEPTED → CONFIRMED → CLOSED/FAILED).
    Secondary: active_lifecycle_trades open rows not already represented.
    """
    path = _triage_v31_path()
    if not path.is_file():
        return []

    from runtime.dual_core_execution import MICRO_TP_POINTS

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        conn = connect_triage_sqlite_readonly(path)
    except Exception:
        return []
    lim = max(1, min(int(limit), 100))
    try:
        conn.row_factory = None
        try:
            cur = conn.execute(
                """
                SELECT deal_reference, deal_id, epic, direction, size, status,
                       created_at, broker_payload
                FROM production_orders
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (lim,),
            )
            for ref, deal_id, epic, direction, size, status, created_at, payload_raw in cur.fetchall():
                key = str(deal_id or ref or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                payload_obj: dict[str, Any] | None = None
                if payload_raw:
                    try:
                        payload_obj = json.loads(str(payload_raw))
                    except Exception:
                        payload_obj = {"raw": str(payload_raw)[:500]}
                terminal = str(status or "").upper() in (
                    "CLOSED",
                    "FAILED",
                    "REJECTED",
                    "TIMEOUT",
                    "CLOSED_ON_BROKER_ANOMALY",
                )
                rows.append(
                    {
                        "timestamp": created_at or _utc_now_iso(),
                        "opened_at": created_at,
                        "epic": epic,
                        "direction": direction,
                        "entry": None,
                        "dealId": deal_id or ref,
                        "deal_id": deal_id or ref,
                        "deal_reference": ref,
                        "size": float(size or 0),
                        "pnl": 0.0,
                        "pnl_gbp": 0.0,
                        "status": status,
                        "terminal": terminal,
                        "broker_payload": payload_obj,
                        "source": "production_orders",
                        "trail_progress_pct": 0.0,
                        "points_to_trail": float(MICRO_TP_POINTS),
                    }
                )
        except Exception:
            pass

        try:
            cur = conn.execute(
                """
                SELECT deal_id, epic, direction, size, broker_level, broker_upl,
                       last_broker_sync_at, lifecycle_state
                FROM active_lifecycle_trades
                WHERE lifecycle_state NOT IN ('CLOSED', 'CLOSED_ON_BROKER_ANOMALY')
                ORDER BY last_broker_sync_at DESC
                """
            )
            for deal_id, epic, direction, size, level, upl, synced_at, state in cur.fetchall():
                did = str(deal_id or "").strip()
                if not did or did in seen:
                    continue
                seen.add(did)
                entry = float(level or 0)
                pnl = float(upl or 0)
                pos = {
                    "timestamp": synced_at or _utc_now_iso(),
                    "opened_at": synced_at,
                    "epic": epic,
                    "direction": direction,
                    "entry": entry,
                    "dealId": did,
                    "deal_id": did,
                    "size": float(size or 0),
                    "pnl": pnl,
                    "pnl_gbp": pnl,
                    "current": entry,
                    "lifecycle_state": state,
                    "status": state,
                    "terminal": False,
                    "source": "active_lifecycle_trades",
                }
                trail_pct, points_to_trail = _trail_metrics_for_position(pos, None)
                pos["trail_progress_pct"] = trail_pct
                pos["points_to_trail"] = points_to_trail
                rows.append(pos)
        except Exception:
            pass
    finally:
        conn.close()

    return rows


def _query_active_positions_from_triage_sql() -> list[dict[str, Any]]:
    """Backward-compatible alias — live production_orders SQL."""
    return _query_live_production_ledger_sql()


def _resolve_rest_for_telemetry() -> Any | None:
    try:
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated, get_shared_rest_client

        cred = try_load_credentials()
        if not cred.ok or cred.credentials is None:
            return None
        client = get_shared_rest_client(cred.credentials)
        session = getattr(client, "session", None)
        if session and getattr(session, "is_valid", False):
            return client
        return ensure_shared_authenticated(cred.credentials)
    except Exception:
        return None


def _rest_spot_mid(rest: Any, epic: str) -> float | None:
    """Fast GET /markets/{epic} snapshot — REST fallback when hub ticks are stale."""
    try:
        cache = getattr(rest, "_market_constraints_cache", None)
        if isinstance(cache, dict):
            cached = cache.get(epic)
            if cached and time.time() - float(cached.get("ts", 0)) < 30.0:
                data = cached.get("data") or {}
                bid = float(data.get("bid") or 0)
                offer = float(data.get("offer") or 0)
                if bid > 0 and offer > 0:
                    return (bid + offer) / 2.0
        data = rest.fetch_market_constraints(epic)
        bid = float(data.get("bid") or 0)
        offer = float(data.get("offer") or 0)
        if bid > 0 and offer > 0:
            return (bid + offer) / 2.0
    except Exception:
        pass
    return None


def reconcile_ledger_with_broker(rest: Any, *, mark_closed: bool = True) -> dict[str, int]:
    """Match triage_v31.db open rows against GET /positions/otc."""
    broker_deals: set[str] = set()
    try:
        for item in rest.open_positions() or []:
            pos = item.get("position") or {}
            did = str(pos.get("dealId") or pos.get("dealID") or "").strip()
            if did and float(pos.get("size") or 0) > 0:
                broker_deals.add(did)
    except Exception:
        return {"broker_open": 0, "local_open": 0, "closed_on_broker": 0, "error": 1}

    path = _triage_v31_path()
    closed_count = 0
    local_open = 0
    if not path.is_file():
        return {"broker_open": len(broker_deals), "local_open": 0, "closed_on_broker": 0}

    conn = connect_triage_sqlite(path)
    try:
        cur = conn.execute(
            """
            SELECT deal_id FROM active_lifecycle_trades
            WHERE lifecycle_state NOT IN ('CLOSED', 'CLOSED_ON_BROKER_ANOMALY')
            """
        )
        local_ids = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        local_open = len(local_ids)
        if mark_closed:
            for did in local_ids:
                if did not in broker_deals:
                    conn.execute(
                        """
                        UPDATE active_lifecycle_trades
                        SET lifecycle_state = 'CLOSED',
                            last_broker_sync_at = ?,
                            last_event = 'closed',
                            notes = 'absent_on_broker'
                        WHERE deal_id = ?
                        """,
                        (_utc_now_iso(), did),
                    )
                    try:
                        conn.execute(
                            """
                            UPDATE production_orders
                            SET status = 'CLOSED'
                            WHERE deal_id = ? AND status NOT IN (
                                'CLOSED', 'FAILED', 'CLOSED_ON_BROKER_ANOMALY'
                            )
                            """,
                            (did,),
                        )
                    except Exception:
                        pass
                    closed_count += 1
            if closed_count:
                conn.commit()
        local_open = max(0, local_open - closed_count)
    finally:
        conn.close()

    return {
        "broker_open": len(broker_deals),
        "local_open": local_open,
        "closed_on_broker": closed_count,
    }


def _enrich_positions_spot_pnl(rows: list[dict[str, Any]], rest: Any) -> list[dict[str, Any]]:
    from trading.open_position_view import extract_broker_profit_and_loss

    broker_upl: dict[str, float | None] = {}
    if rest is not None:
        try:
            for item in rest.open_positions() or []:
                pos = item.get("position") or {}
                did = str(pos.get("dealId") or pos.get("dealID") or "").strip()
                if not did:
                    continue
                upl, _ = extract_broker_profit_and_loss(pos)
                broker_upl[did] = float(upl) if upl is not None else None
        except Exception:
            pass

    hub = get_market_data_hub()
    for pos in rows:
        did = str(pos.get("deal_id") or pos.get("dealId") or "")
        epic = str(pos.get("epic") or "")
        entry = float(pos.get("entry") or 0)
        direction = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)

        if did in broker_upl and broker_upl[did] is not None:
            pos["pnl"] = pos["pnl_gbp"] = round(broker_upl[did], 4)
            pos["pnl_source"] = "broker_upl"
            continue

        spot: float | None = None
        pnl_source = "computed"
        snap = hub.get_snapshot(epic)
        if snap is not None and snap.bid > 0 and snap.offer > 0 and snap.age_seconds() <= 45.0:
            spot = (float(snap.bid) + float(snap.offer)) / 2.0
            pnl_source = "hub_spot"
        elif rest is not None:
            spot = _rest_spot_mid(rest, epic)
            if spot:
                pnl_source = "rest_fallback"

        if spot is not None and entry > 0 and size > 0:
            sign = 1.0 if direction == "BUY" else -1.0
            pnl = (spot - entry) * sign * size
            pos["current"] = round(spot, 5)
            pos["pnl"] = pos["pnl_gbp"] = round(pnl, 4)
            pos["pnl_source"] = pnl_source
            trail_pct, points_to_trail = _trail_metrics_for_position(pos, rest)
            pos["trail_progress_pct"] = trail_pct
            pos["points_to_trail"] = points_to_trail
    return rows


_warm_fallback_last_ts: float = 0.0
_WARM_FALLBACK_MIN_SEC = 15.0


def _warm_stack_from_rest_fallback() -> None:
    """Feed dual-core Z pipeline when hub ticks are missing — debounced REST."""
    global _warm_fallback_last_ts
    now = time.time()
    if now - _warm_fallback_last_ts < _WARM_FALLBACK_MIN_SEC:
        return
    try:
        from runtime.dual_core_execution import (
            get_active_stack_epics,
            ingest_hub_mid,
            refresh_stacked_dual_assets,
        )

        rest = _resolve_rest_for_telemetry()
        if rest is None:
            return
        hub = get_market_data_hub()
        for epic in get_active_stack_epics():
            snap = hub.get_snapshot(epic)
            stale = (
                snap is None
                or snap.bid <= 0
                or snap.offer <= 0
                or snap.age_seconds() > 45.0
            )
            if stale:
                mid = _rest_spot_mid(rest, epic)
                if mid and mid > 0:
                    ingest_hub_mid(epic, mid)
        refresh_stacked_dual_assets()
        _warm_fallback_last_ts = now
    except Exception:
        pass


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
        # Hot telemetry path — never block on live GET /accounts refresh.
        out["account_balance_gbp"] = summary.get("balance")
        out["account_available_gbp"] = summary.get("available")
        out["account_profit_loss_gbp"] = summary.get("profit_loss")
    except Exception:
        pass
    return out


def _probe_broker_network_rtt_ms(*, min_interval_sec: float = 15.0) -> float | None:
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
    cached = _rtt_probe_cache.get("rtt_ms")
    if cached is None:
        try:
            probed = _probe_broker_network_rtt_ms()
            if probed is not None:
                cached = probed
        except Exception:
            cached = None
    rtt = float(cached) if cached is not None else None
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
        get_failover_state,
        get_stacked_asset_channels,
        get_z_score_stream,
        resolve_core_b_gate_stack,
    )

    boot_gate = resolve_active_boot_gate()
    snap = get_dual_core_snapshot()
    alpha = resolve_ml_alpha_weight()
    gate_stack: dict[str, Any] = {}
    if boot_gate is None:
        gate_stack = resolve_core_b_gate_stack()
    focus = get_execution_focus_state()
    failover = get_failover_state()
    stacked = focus.get("stacked_asset_channels") or get_stacked_asset_channels()
    return {
        "live_calculated_zscore": round(float(snap.live_calculated_zscore), 4),
        "ml_alpha_weight": alpha,
        "ticks_processed": resolve_ticks_processed(),
        "active_positions": build_active_positions_array(),
        "z_score_stream": get_z_score_stream(),
        "stacked_dual_asset_mode": True,
        "stacked_asset_channels": stacked,
        **failover,
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
        "boot_gate": boot_gate,
    }


def resolve_cognitive_router_fields() -> dict[str, Any]:
    alpha = resolve_ml_alpha_weight()
    boot_gate = resolve_active_boot_gate()
    trend = resolve_live_15min_macro_trend() if boot_gate is None else "UNKNOWN"
    return {
        "live_15min_macro_trend": trend,
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


def build_v31_telemetry_lite() -> dict[str, Any]:
    """Sub-100ms dashboard poll — hub quotes + snapshot fields only."""
    from runtime.dual_core_execution import (
        get_dual_core_snapshot,
        get_failover_state,
        get_stacked_asset_channels,
        get_z_score_stream,
    )

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
    core = get_dual_core_snapshot()
    failover = get_failover_state()
    stacked = get_stacked_asset_channels()
    boot_gate = resolve_active_boot_gate()
    account = resolve_account_capital_fields()
    transport = resolve_transport_telemetry_fields()
    ledger_meta: dict[str, Any] = {}
    try:
        from runtime.ledger_hydration_core import ledger_hydration_state

        ledger_meta = ledger_hydration_state()
    except Exception:
        ledger_meta = {}
    active_positions = build_active_positions_array()
    return {
        "ok": True,
        "ts": _utc_now_iso(),
        "assets": assets,
        "asset_count": len(assets),
        "fresh_count": fresh,
        "execution_mode": core.execution_mode,
        "volatility_z_score": round(float(core.volatility_z_score), 4),
        "live_calculated_zscore": round(float(core.live_calculated_zscore), 4),
        "dual_core_status": {
            "core_b_micro_active": bool(core.core_b_micro_active),
            "execution_mode": core.execution_mode,
            "volatility_z_score": round(float(core.volatility_z_score), 4),
        },
        "positions_db_source": (
            ledger_meta.get("ledger_hydration_source")
            if ledger_meta.get("ledger_synced")
            else f"{_triage_v31_path()}::production_orders"
        ),
        "ledger_query": (
            "ig_history_hydration_cache"
            if ledger_meta.get("ledger_synced")
            else "production_orders_live_sql"
        ),
        "live_15min_macro_trend": "UNKNOWN",
        "ml_alpha_weight": None,
        "ml_current_alpha_weight": None,
        "last_gate_suppression_reason": "",
        "ticks_processed": resolve_ticks_processed(),
        "active_positions": active_positions,
        **ledger_meta,
        "z_score_stream": get_z_score_stream(),
        "stacked_dual_asset_mode": True,
        "stacked_asset_channels": stacked,
        **failover,
        "gate_stack_matrix": {},
        "boot_gate": boot_gate,
        **account,
        **transport,
    }


def build_v31_telemetry() -> dict[str, Any]:
    """Core asset quote map from MarketDataHub (non-blocking read)."""
    t_build = time.perf_counter()
    # REST stack-warm runs in DualCoreCoordinator — not on 1s dashboard poll.
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
        "positions_db_source": f"{_triage_v31_path()}::production_orders",
        "ledger_query": "production_orders_live_sql",
        **cognitive,
        **high_speed,
        **account,
        **transport,
    }


_DASHBOARD_TELEMETRY_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "data": {"ok": False, "degraded": True, "active_positions": []},
}
_DASHBOARD_TELEMETRY_LOCK = threading.Lock()
_DASHBOARD_TELEMETRY_TTL_SEC = 0.9


def get_dashboard_telemetry() -> dict[str, Any]:
    """Cached telemetry for 1s dashboard poll — single-flight refresh."""
    global _DASHBOARD_TELEMETRY_CACHE
    now = time.time()
    cached = _DASHBOARD_TELEMETRY_CACHE.get("data") or {}
    if now - float(_DASHBOARD_TELEMETRY_CACHE.get("ts") or 0.0) < _DASHBOARD_TELEMETRY_TTL_SEC and cached:
        return dict(cached)
    if not _DASHBOARD_TELEMETRY_LOCK.acquire(blocking=False):
        if cached:
            stale = dict(cached)
            stale["degraded"] = True
            return stale
        return {"ok": False, "degraded": True, "active_positions": [], "ts": _utc_now_iso()}
    try:
        now = time.time()
        cached = _DASHBOARD_TELEMETRY_CACHE.get("data") or {}
        if now - float(_DASHBOARD_TELEMETRY_CACHE.get("ts") or 0.0) < _DASHBOARD_TELEMETRY_TTL_SEC and cached:
            return dict(cached)
        try:
            fresh = build_v31_telemetry_lite()
        except Exception:
            if cached:
                stale = dict(cached)
                stale["degraded"] = True
                return stale
            raise
        _DASHBOARD_TELEMETRY_CACHE = {"ts": now, "data": fresh}
        return dict(fresh)
    finally:
        _DASHBOARD_TELEMETRY_LOCK.release()


def build_v31_positions() -> dict[str, Any]:
    """Live open contracts — direct triage_v31.db SQL (no sync snapshot cache)."""
    rows = _query_active_positions_from_triage_sql()
    position_map = {
        str(r.get("dealId") or r.get("deal_id") or ""): r for r in rows if r.get("dealId") or r.get("deal_id")
    }
    return {
        "ok": True,
        "ts": _utc_now_iso(),
        "total_open": len(rows),
        "positions": position_map,
        "sync_status": "triage_sql_live",
        "last_sync_at": _utc_now_iso(),
        "db_path": str(_triage_v31_path()),
    }


def build_v31_history(*, limit: int = 10) -> dict[str, Any]:
    """Latest closed trade outcomes from triage_v31.db."""
    lim = max(1, min(int(limit), 50))
    path = _triage_v31_path()
    if not path.is_file():
        return {"ok": True, "ts": _utc_now_iso(), "rows": [], "count": 0}

    try:
        conn = connect_triage_sqlite_readonly(path)
    except Exception:
        return {"ok": True, "ts": _utc_now_iso(), "rows": [], "count": 0}
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
