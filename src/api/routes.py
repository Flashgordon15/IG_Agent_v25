"""
HTTP routes — dashboard API (Section 4.5 Steps 8 + 13).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse, Response

from api.agent_control import (
    is_paused,
    is_trading_running,
    run_emergency_stop,
    start_trading,
    stop_trading,
)
from api.agent_health import get_cached_health_status
from api.close_handler import close_deal
from api.dashboard_data import (
    dismiss_splash,
    get_closed_trades,
    get_shadow_closed_trades,
    get_signal_log,
    get_system_info,
    read_version_state,
    run_e2e_execution_check,
    run_safe_to_leave,
    run_system_tests,
)
from api.intelligence_data import (
    intelligence_dashboard,
    learning_status,
    replay_summary,
    run_replay_pipeline,
    shadow_today,
)
from api.snapshot_store import get_tick, snapshot_age_s_fast

_T = TypeVar("_T")
# Isolated from G5/boot asyncio.to_thread pool — prevents dashboard poll starvation.
_DASHBOARD_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dashboard-api")


async def _run_dashboard_sync(
    fn: Callable[..., _T],
    *args: Any,
    timeout: float | None = 2.5,
    **kwargs: Any,
) -> _T:
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(
        _DASHBOARD_EXECUTOR,
        lambda: fn(*args, **kwargs),
    )
    if timeout is None:
        return await fut
    return await asyncio.wait_for(fut, timeout=timeout)


# ── Heartbeat ────────────────────────────────────────────────────────────────
# Browser pings /api/heartbeat every 30 s. The endpoint is kept so the
# dashboard can use it as a liveness indicator, but missed pings no longer
# trigger a shutdown. Use POST /api/shutdown for deliberate agent termination.
HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TIMEOUT_SEC = 600  # retained for reference; not used for shutdown
_last_heartbeat: float = time.time()
_heartbeat_lock = threading.Lock()

from api.testbed_simulation import router as testbed_router

router = APIRouter()
router.include_router(testbed_router)


@router.get("/health")
async def health() -> dict[str, Any]:
    """Fast liveness — avoid sync thread-pool dispatch under boot load."""
    t0 = time.perf_counter()
    age = snapshot_age_s_fast()
    payload = {
        "ok": True,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "api": "up",
        "snapshot_age_s": age,
    }
    try:
        from apex.system_monitor import record_health_ping

        port = int(os.environ.get("IG_API_PORT", "9090"))
        ms = (time.perf_counter() - t0) * 1000.0
        record_health_ping(port=port, latency_ms=ms, ok=True)
    except Exception:
        pass
    return payload


@router.get("/api/health")
async def api_health() -> JSONResponse:
    """Incremental health from async snapshot — non-blocking under tick load."""
    import time

    from api.endpoint_profiler import record_request
    from api.readiness_snapshot import get_health_snapshot

    t0 = time.perf_counter()
    code, body = get_health_snapshot()
    body["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    record_request("health", (time.perf_counter() - t0) * 1000.0)
    return JSONResponse(status_code=code, content=body)


@router.get("/api/boot_status")
def api_boot_status() -> JSONResponse:
    """Lightweight boot pipeline snapshot — stages, subsystems, trade_ready."""
    from api.boot_status import api_boot_status_json

    return api_boot_status_json()


@router.get("/api/boot_log")
def api_boot_log(limit: int = 100) -> JSONResponse:
    """Recent boot events for cockpit splash diagnostics."""
    from api.boot_status import api_boot_log_json

    return api_boot_log_json(limit=min(max(limit, 1), 200))


@router.get("/api/unified_status")
def api_unified_status() -> JSONResponse:
    """Full unified runtime snapshot for IG Cockpit."""
    from api.unified_status import api_unified_status_json

    return api_unified_status_json()


@router.get("/api/trade_lifecycle")
def api_trade_lifecycle() -> JSONResponse:
    """Active trade lifecycle state machine + bus snapshot."""
    from api.unified_status import api_trade_lifecycle_json

    return api_trade_lifecycle_json()


@router.get("/api/rejections")
def api_rejections(limit: int = 20) -> JSONResponse:
    """Recent classified IG rejections."""
    from api.unified_status import api_rejections_json

    return api_rejections_json(limit=min(max(limit, 1), 100))


@router.get("/api/rotation_status")
def api_rotation_status() -> JSONResponse:
    """Market rotation sweep state."""
    from api.unified_status import api_rotation_status_json

    return api_rotation_status_json()


@router.get("/api/trade_state")
def api_trade_state() -> JSONResponse:
    """Full trade state — lifecycle, stops, dynamic limits, sizing."""
    from api.trade_state_api import api_trade_state_json

    return api_trade_state_json()


@router.get("/api/position_risk_monitor")
def api_position_risk_monitor() -> dict[str, Any]:
    """Audit open positions vs armed GBP/virtual/dynamic risk tracks."""
    from api.position_risk_monitor import build_position_risk_report

    return build_position_risk_report()


@router.get("/api/positions/live")
async def api_positions_live() -> dict[str, Any]:
    """Fast IG-sync open book for terminal UI — cache-only, 2s hard cap."""
    import asyncio

    from api.positions_live import (
        build_live_positions_payload,
        last_good_live_positions_payload,
    )

    try:
        return await _run_dashboard_sync(build_live_positions_payload, timeout=2.0)
    except asyncio.TimeoutError:
        # Never invent an empty flat book on timeout when last-good had opens.
        return last_good_live_positions_payload(error="timeout")


@router.get("/api/desk/why_idle")
def api_desk_why_idle(heal: bool = False) -> dict[str, Any]:
    """Self-assessment — why entries are idle + optional Yahoo hub bridge heal."""
    from runtime.desk_self_assess import run_self_assess_tick

    return run_self_assess_tick(heal=bool(heal))


@router.get("/api/desk/simplified_accounting")
def api_desk_simplified_accounting() -> dict[str, Any]:
    """Sovereign accounting board — today net / last 10 / daily history + health."""
    try:
        from diagnostics.performance_journal import simplified_accounting_payload

        return simplified_accounting_payload()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "today_net_realized_pnl_gbp": 0.0,
            "last_10_closed_trades": [],
            "daily_history": [],
            "empty_day": True,
            "system_state": {"is_healthy": False, "operational_badge": False},
        }


@router.get("/api/desk/weekly_metrics")
def api_desk_weekly_metrics() -> dict[str, Any]:
    """Rolling 7-day desk ledger — Sharpe, asymmetric PF, per-account assets."""
    try:
        from analytics.weekly_performance_ledger import WeeklyPerformanceLedger

        return WeeklyPerformanceLedger.compile_weekly_metrics()
    except Exception as exc:
        empty = {
            "weekly_sharpe": None,
            "asymmetric_profit_factor": 0.0,
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "gross_wins_gbp": 0.0,
            "gross_losses_gbp": 0.0,
            "net_pnl_gbp": 0.0,
            "sample_n": 0,
            "trading_days": 0,
            "asset_breakdown": [],
        }
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "week_start": "",
            "week_end": "",
            "merged": dict(empty),
            "accounts": {},
            "asset_breakdown": [],
        }


@router.get("/api/kernel/shm_snapshot")
def api_kernel_shm_snapshot() -> dict[str, Any]:
    """v33 SHM ring buffer snapshot — UI multiplex / desk polling (no disk)."""
    try:
        from kernel.shm_facade import snapshot_payload

        return snapshot_payload()
    except Exception as exc:
        return {
            "ok": False,
            "attached": False,
            "error": f"{type(exc).__name__}:{exc}",
            "positions": [],
            "stats": {},
        }


@router.get("/api/desk/rest_budget")
def api_desk_rest_budget() -> dict[str, Any]:
    """Cross-process + in-process REST budget snapshot for desk operators."""
    out: dict[str, Any] = {"ok": True}
    try:
        from system.rest_api_budget import get_rest_api_budget

        metrics = get_rest_api_budget().metrics()
        out["rest_api_budget"] = metrics
        out["pressure_level"] = str(metrics.get("pressure_level") or "IDLE")
        # Align with ops_strip / entries_blocked_by_rest_pressure — ELEVATED
        # already pauses NEW entries (budget reserved for closes).
        out["rest_pressure"] = out["pressure_level"] in (
            "ELEVATED",
            "HIGH",
            "CRITICAL",
        )
    except Exception as exc:
        out["rest_api_budget_error"] = f"{type(exc).__name__}"
        out["pressure_level"] = "UNKNOWN"
        out["rest_pressure"] = False
    try:
        from system import shared_rest_budget

        out["shared"] = shared_rest_budget.snapshot()
    except Exception as exc:
        out["shared_error"] = f"{type(exc).__name__}"
    try:
        from system import chaos_guardian

        snap = getattr(chaos_guardian, "snapshot", None)
        if callable(snap):
            out["token_buckets"] = (snap() or {}).get("token_buckets") or {}
        else:
            out["token_buckets"] = (getattr(chaos_guardian, "_snapshot", {}) or {}).get(
                "token_buckets"
            ) or {}
    except Exception:
        out["token_buckets"] = {}
    return out


def _desk_idle_reason_for_ops() -> dict[str, Any] | None:
    """One clear desk-level idle reason (slot / bars / gate) — not four UI copies."""
    import time as _time

    try:
        from runtime.intraday_slot_tracker import (
            intraday_slots_enabled,
            slot_id_for_timestamp,
        )

        cfg = None
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
        if intraday_slots_enabled(cfg):
            sid = slot_id_for_timestamp(_time.time(), cfg) or ""
            if sid == "us_close":
                try:
                    from system.strategy_quality_gate import evaluate_entry_slot_gate

                    slot_ok, _slot_detail = evaluate_entry_slot_gate(cfg)
                    if not slot_ok:
                        return {
                            "code": "us_close",
                            "label": "US Close session window — desk idle is expected, not a crash",
                        }
                except Exception:
                    return {
                        "code": "us_close",
                        "label": "US Close session window — desk idle is expected, not a crash",
                    }
            # Surface strategy_quality slot blocks as a clear desk idle reason
            try:
                from system.strategy_quality_gate import evaluate_entry_slot_gate

                ok_slot, slot_detail = evaluate_entry_slot_gate(cfg)
                if not ok_slot:
                    return {
                        "code": "slot_blocked",
                        "label": str(slot_detail or f"slot {sid} blocked"),
                    }
            except Exception:
                pass
    except Exception:
        pass
    try:
        from system.regime_state import get_regime_state_snapshot

        snap = get_regime_state_snapshot() or {}
        for m in snap.get("markets") or []:
            if not isinstance(m, dict):
                continue
            if str(m.get("epic") or "") != "IX.D.DOW.IFM.IP":
                continue
            reason = str(m.get("reason") or "")
            gate = m.get("strategy_gate") if isinstance(m.get("strategy_gate"), dict) else {}
            if "insufficient" in reason.lower():
                return {
                    "code": "insufficient_bars",
                    "label": "DOW warming — insufficient bars for regime / strategy gate",
                }
            if gate.get("allow_entries") is False:
                mode = str(gate.get("mode") or reason or "gated")
                return {
                    "code": "entries_gated",
                    "label": f"DOW entries gated ({mode})",
                }
            break
    except Exception:
        pass
    try:
        from system.strategy_quality_gate import evaluate_desk_halt_gate

        # Live gate — never trust a stale desk_self_assess WR halt string.
        ok_halt, halt_detail, _halt_val = evaluate_desk_halt_gate()
        if not ok_halt:
            return {
                "code": "strategy_quality",
                "label": str(halt_detail or "strategy quality desk halt"),
            }
    except Exception:
        pass
    try:
        from runtime.desk_self_assess import last_assessment

        assess = last_assessment() or {}
        primary = assess.get("primary_blocker") if assess.get("idle") else None
        if isinstance(primary, dict) and primary.get("id"):
            # Skip stale strategy_quality when live gate is clear.
            if str(primary.get("id")) == "strategy_quality":
                return None
            return {
                "code": str(primary.get("id")),
                "label": str(primary.get("detail") or primary.get("id")),
            }
    except Exception:
        pass
    return None


@router.get("/api/desk/ops_strip")
def api_desk_ops_strip() -> dict[str, Any]:
    """Read-only Trading Desk status strip — milestone + ATR R:R + macro bias.

    Non-blocking disk/config peek for the Quantum Terminal UI. Never touches
    the Lightstreamer hot path or REST budget.
    """
    out: dict[str, Any] = {
        "ok": True,
        "core_detached": False,
        "maintenance_detached_badge": None,
        "daily_realized_pnl_gbp": 0.0,
        "daily_milestone_gbp": 1000.0,
        "progress_pct": 0.0,
        "atr_reward_risk": 3.5,
        "grok_macro_bias": "NEUTRAL",
        "desk_idle_reason": None,
        "trading_path_live": False,
        "trading_path_blockers": [],
        "trading_path_badge": "DESK TRADING DOWN — readiness unknown",
    }
    try:
        from diagnostics.performance_journal import milestone_progress_payload

        mile = milestone_progress_payload()
        out.update(
            {
                "daily_realized_pnl_gbp": mile.get("daily_realized_pnl_gbp", 0.0),
                "daily_milestone_gbp": mile.get("daily_milestone_gbp", 1000.0),
                "progress_pct": mile.get("progress_pct", 0.0),
                "journal": mile.get("journal"),
                "benchmark": mile.get("benchmark"),
            }
        )
    except Exception as exc:
        out["milestone_error"] = f"{type(exc).__name__}"
    try:
        from execution.grok_macro_bias import resolve_grok_macro_bias

        out["grok_macro_bias"] = resolve_grok_macro_bias()
    except Exception:
        pass
    try:
        from diagnostics.param_tuner import load_overlay_cached

        overlay = load_overlay_cached() or {}
        vb = overlay.get("volatility_bracket") or {}
        rr = vb.get("elevated_vol_reward_risk")
        if rr is not None:
            out["atr_reward_risk"] = float(rr)
    except Exception:
        pass
    try:
        from alpha.micro_sniper_ml import latest_sniper_ml_snapshot

        # Ops strip badge must reflect hot-path (DOW) sniper only — never show
        # a stale Nikkei/Gold/FX cache hit as the desk arm signal.
        hot_epic = "IX.D.DOW.IFM.IP"
        try:
            from system.config_loader import get_config

            cfg = get_config()
            dual = cfg.get("dual_core") if hasattr(cfg, "get") else {}
            excluded = {
                str(e).strip()
                for e in ((dual or {}).get("exclude_from_hot_path") or [])
            }
            raw_hot = (dual or {}).get("hot_path_epics") or []
            candidates = [str(e).strip() for e in raw_hot if str(e).strip()]
            if not candidates:
                candidates = [hot_epic]
            hot_candidates = [e for e in candidates if e not in excluded] or [
                e for e in [hot_epic] if e not in excluded
            ]
        except Exception:
            hot_candidates = [hot_epic]

        snap = None
        for epic in hot_candidates:
            row = latest_sniper_ml_snapshot(epic=epic)
            if row.get("p_success") is not None and str(row.get("epic") or "") == epic:
                snap = row
                break
        if snap is None:
            # Fall back to global only when it already matches a hot-path epic.
            global_snap = latest_sniper_ml_snapshot()
            g_epic = str(global_snap.get("epic") or "")
            if global_snap.get("p_success") is not None and g_epic in set(
                hot_candidates
            ):
                snap = global_snap
        if snap is not None and snap.get("p_success") is not None:
            out["sniper_ml"] = {
                "p_success": snap.get("p_success"),
                "approved": snap.get("approved"),
                "threshold": snap.get("threshold"),
                "epic": snap.get("epic"),
                "ts": snap.get("ts"),
            }
    except Exception:
        pass
    try:
        out["desk_idle_reason"] = _desk_idle_reason_for_ops()
    except Exception:
        out["desk_idle_reason"] = None
    try:
        from execution.maintenance_detachment import is_core_detached

        detached = is_core_detached()
        out["core_detached"] = detached
        if detached:
            out["maintenance_detached_badge"] = (
                "[🛠️ MAINTENANCE DEVELOPMENT MODE - TRADING DETACHED]"
            )
    except Exception:
        out["core_detached"] = False
    try:
        from runtime.trading_path_readiness import compute_trading_path_readiness

        path = compute_trading_path_readiness(desk_idle=out.get("desk_idle_reason"))
        out["trading_path_live"] = bool(path.get("trading_path_live"))
        out["trading_path_blockers"] = list(path.get("blockers") or [])
        out["trading_path_badge"] = str(path.get("badge") or "")
        out["trading_path_primary"] = path.get("primary_blocker")
        out["trading_path"] = path
    except Exception as exc:
        out["trading_path_error"] = f"{type(exc).__name__}"
    # REST pressure — never show false OK when the governor is hot.
    # Include ELEVATED (entries paused) and CAP BREACH from snapshot SoT.
    try:
        from system.rest_api_budget import get_rest_api_budget

        metrics = get_rest_api_budget().metrics()
        level = str(metrics.get("pressure_level") or "IDLE")
        out["rest_pressure_level"] = level
        out["rest_pressure"] = level in ("ELEVATED", "HIGH", "CRITICAL")
        out["rest_calls_last_minute"] = int(metrics.get("calls_last_minute") or 0)
        if out["rest_pressure"]:
            out["rest_pressure_warning"] = (
                f"REST PRESSURE {level} — {metrics.get('status_label')} "
                f"(entries paused; closes reserved)"
            )
    except Exception:
        out["rest_pressure"] = False
        out["rest_pressure_level"] = "UNKNOWN"

    # Cap breach badge from snapshot (authoritative under coalesce).
    try:
        from runtime.broker_snapshot import open_count_from_snapshot
        from runtime.desk_stability_harness import boot_grace_active
        from system.config_loader import get_config

        cfg = get_config()
        max_open = max(1, int(getattr(cfg, "max_open_positions", 6) or 6))
        snap_n = open_count_from_snapshot(max_age_sec=300.0)
        out["broker_open_snapshot"] = snap_n
        out["max_open_positions"] = max_open
        live_open = 0
        try:
            live_open = int(out.get("broker_open") or 0)
        except (TypeError, ValueError):
            live_open = 0
        stale_cap_skip = boot_grace_active() and live_open <= 0
        if snap_n is not None and snap_n > max_open and not stale_cap_skip:
            out["cap_breach"] = True
            out["cap_breach_warning"] = (
                f"CAP BREACH broker_open={snap_n}>{max_open} — flatten before entries"
            )
        else:
            out["cap_breach"] = False
    except Exception:
        out["cap_breach"] = False

    # Compose badge — CAP BREACH / REST PRESSURE win over green path.
    badge_bits: list[str] = []
    if out.get("cap_breach"):
        badge_bits.append(str(out.get("cap_breach_warning") or "CAP BREACH"))
    if out.get("rest_pressure"):
        badge_bits.append(str(out.get("rest_pressure_warning") or "REST PRESSURE"))
    if badge_bits:
        if out.get("trading_path_live"):
            out["trading_path_badge"] = " | ".join(
                ["PATH LIVE"] + badge_bits
            )
        else:
            base = str(out.get("trading_path_badge") or "DESK TRADING DOWN")
            out["trading_path_badge"] = f"{base} — " + " | ".join(badge_bits)

    # Composite desk R/A/G — single traffic light for Terminal header / ops_strip.
    # Never treat health.ok alone as green — path / SoT / liveness are separate.
    try:
        from runtime.desk_dev_controls import entries_paused

        paused = bool(entries_paused())
    except Exception:
        paused = False
    out["entries_paused"] = paused
    level = str(out.get("rest_pressure_level") or "IDLE").upper()
    path_live = bool(out.get("trading_path_live"))
    if out.get("cap_breach") or level == "CRITICAL":
        rag, rag_label = "R", "RED — cap/REST critical"
    elif paused or out.get("rest_pressure") or level in ("HIGH", "ELEVATED"):
        rag, rag_label = "A", "AMBER — entries paused or REST pressure"
    elif path_live:
        rag, rag_label = "G", "GREEN — path live"
    else:
        rag, rag_label = "A", "AMBER — path not live"
    out["desk_rag"] = rag
    out["desk_rag_label"] = rag_label

    # Broker SoT + desk liveness — surfaced separately so UI never greenwashes.
    sot: dict[str, Any] = {
        "count": out.get("broker_open_snapshot"),
        "source": "broker_snapshot",
        "ok": True,
    }
    try:
        from runtime.broker_snapshot import open_count_from_snapshot
        from runtime.desk_stability_harness import trade_support_stale_budget_sec

        snap_n = open_count_from_snapshot(max_age_sec=300.0)
        ts_path = None
        sot_budget = trade_support_stale_budget_sec()
        try:
            from system.paths import data_dir

            ts_file = data_dir() / "trade_support_status.json"
            if ts_file.is_file():
                import json as _json
                import time as _time

                raw = _json.loads(ts_file.read_text(encoding="utf-8"))
                age = _time.time() - float(raw.get("ts") or 0)
                sot = {
                    "count": int(raw.get("broker_open") or 0),
                    "source": "trade_support",
                    "status_age_sec": round(age, 1),
                    "ok": age < sot_budget,
                    "stale_budget_sec": sot_budget,
                    "snapshot_count": snap_n,
                }
                ts_path = True
        except Exception:
            ts_path = False
        if not ts_path:
            sot = {
                "count": snap_n,
                "source": "broker_snapshot",
                "ok": snap_n is not None,
            }
    except Exception:
        pass
    out["broker_open_sot"] = sot

    liveness: dict[str, Any] = {"ok": None, "has_open_risk": None}
    try:
        from runtime.trading_desk_liveness import evaluate_liveness

        liv = evaluate_liveness()
        if isinstance(liv, dict):
            liveness = {
                "ok": bool(liv.get("ok")),
                "has_open_risk": bool(liv.get("has_open_risk")),
                "open_count": liv.get("open_count"),
                "issues": list(liv.get("issues") or [])[:6],
            }
    except Exception:
        liveness = {"ok": None, "has_open_risk": None, "source": "unavailable"}
    out["desk_liveness"] = liveness

    try:
        from runtime.feed_transport_summary import build_feed_transport_summary

        out["feed_transport_summary"] = build_feed_transport_summary()
    except Exception as exc:
        out["feed_transport_summary"] = {
            "label": "FEED — unavailable",
            "error": f"{type(exc).__name__}",
        }

    # Application Stability Harness — observe-only on ops_strip (no side effects).
    desk_stability: dict[str, Any] = {}
    try:
        from runtime.desk_stability_harness import (
            evaluate_stability,
            latest_stability,
        )

        cached = latest_stability()
        if cached.get("desk_stability"):
            desk_stability = dict(cached["desk_stability"])
        else:
            # Lightweight: reuse already-collected strip fields when possible
            snap = evaluate_stability(act=False, in_process=True)
            desk_stability = dict(snap.get("desk_stability") or {})
    except Exception as exc:
        desk_stability = {
            "grade": "A",
            "reasons": [f"harness_unavailable:{type(exc).__name__}"],
            "label": "A — harness unavailable",
        }
    out["desk_stability"] = desk_stability
    # Surface boot latency buffer for Terminal ops badge (30s hydration grace).
    try:
        boot_gate = desk_stability.get("boot_gate") if isinstance(desk_stability, dict) else None
        if isinstance(boot_gate, dict):
            out["boot_gate"] = {
                "boot_started_at": boot_gate.get("boot_started_at"),
                "boot_latency_buffer_sec": boot_gate.get("boot_latency_buffer_sec"),
                "boot_latency_buffer_active": boot_gate.get("boot_latency_buffer_active"),
                "false_engine_blockage_suppressed": boot_gate.get(
                    "false_engine_blockage_suppressed"
                ),
                "boot_grace_active": boot_gate.get("boot_grace_active"),
                "sot_stale_budget_sec": boot_gate.get("sot_stale_budget_sec"),
            }
    except Exception:
        pass

    try:
        from system.agent_orchestration import get_orchestrator_status

        orch = get_orchestrator_status()
        out["orchestrator"] = orch
        out["healing_active"] = bool(orch.get("healing_active"))
        out["dual_engine_operational"] = bool(orch.get("dual_engine_operational"))
        out["order_mutex"] = orch.get("order_mutex") or {}
        out["mutex_reconcile"] = orch.get("mutex_reconcile")
    except Exception as exc:
        out["orchestrator"] = {"ok": False, "error": f"{type(exc).__name__}"}
        out["healing_active"] = False
        try:
            from execution.order_in_flight_mutex import get_order_mutex

            out["order_mutex"] = get_order_mutex().status()
        except Exception:
            out["order_mutex"] = {}

    # Ops enrichment — streak cooldown, hour gate, last ML score, halt SoT flags.
    try:
        from execution.streak_protection import streak_protection_status
        from system.engine_lane import resolve_journal_metadata

        meta = resolve_journal_metadata()
        acct = str(meta.get("account_id") or "")
        streak = streak_protection_status(acct) if acct else {"active": False}
        out["streak_protection"] = streak
        out["streak_cooldown_remaining_sec"] = max(
            float(streak.get("post_win_remaining_sec") or 0),
            float(streak.get("post_loss_remaining_sec") or 0),
        )
    except Exception:
        out["streak_protection"] = {"active": False}
        out["streak_cooldown_remaining_sec"] = 0.0
    try:
        from system.config_loader import get_config
        from system.strategy_quality_gate import evaluate_entry_hour_gate

        cfg = get_config()
        hour_ok, hour_reason, hour_meta = evaluate_entry_hour_gate(
            "IX.D.DOW.IFM.IP", cfg=cfg
        )
        out["hour_gate"] = {
            "ok": bool(hour_ok),
            "reason": str(hour_reason or ""),
            "meta": hour_meta if isinstance(hour_meta, dict) else {},
        }
    except Exception as exc:
        out["hour_gate"] = {"ok": True, "reason": f"unavailable:{type(exc).__name__}"}
    try:
        sniper = out.get("sniper_ml") if isinstance(out.get("sniper_ml"), dict) else {}
        out["last_ml_score"] = sniper.get("p_success")
    except Exception:
        out["last_ml_score"] = None
    try:
        from runtime.halt_sot import halt_status_snapshot

        halt = halt_status_snapshot()
        out["halt_sot"] = halt
        out["halt_active"] = bool(halt.get("entries_halted") or halt.get("deploy_hold_active"))
        out["halt_flags"] = halt.get("flags") or {}
    except Exception:
        out["halt_sot"] = {}
        out["halt_active"] = bool(paused)
        out["halt_flags"] = {}

    out["composite_status"] = {
        "rag": rag,
        "label": rag_label,
        "path_live": path_live,
        "rest": level,
        "entries_paused": paused,
        "cap_breach": bool(out.get("cap_breach")),
        "opens": out.get("broker_open_snapshot"),
        "sot_count": sot.get("count"),
        "sot_ok": sot.get("ok"),
        "liveness_ok": liveness.get("ok"),
        "stability_grade": desk_stability.get("grade"),
        "halt_active": bool(out.get("halt_active")),
        "streak_cooldown_remaining_sec": out.get("streak_cooldown_remaining_sec"),
        "hour_gate_ok": (out.get("hour_gate") or {}).get("ok"),
        "last_ml_score": out.get("last_ml_score"),
        # Explicit: health.ok must never be inferred from this strip alone.
        "health_ok_is_not_desk_green": True,
    }
    return out


@router.get("/api/desk/orchestrator")
def api_desk_orchestrator() -> dict[str, Any]:
    """v33 self-healing orchestrator status — healing_active for Terminal badge."""
    try:
        from system.agent_orchestration import get_orchestrator_status

        return get_orchestrator_status()
    except Exception as exc:
        return {
            "ok": False,
            "healing_active": False,
            "dual_engine_operational": False,
            "error": f"{type(exc).__name__}:{exc}",
        }


@router.get("/api/desk/stability")
def api_desk_stability(act: bool = False) -> dict[str, Any]:
    """Application Stability Harness — composite grade + component SoT.

    Default observe-only. Pass ``?act=1`` to allow rate-limited safe heals
    (UI restart / trade_support heal / pause on REST CRITICAL) — never flatten,
    never kill main.
    """
    try:
        from runtime.desk_stability_harness import evaluate_stability

        return evaluate_stability(act=bool(act), in_process=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "desk_stability": {
                "grade": "R",
                "reasons": [f"evaluate_failed:{type(exc).__name__}"],
                "actions_taken": [],
                "components": {},
            },
        }


@router.get("/api/desk/sniper_ml")
def api_desk_sniper_ml() -> dict[str, Any]:
    """Live QuantumSniperMLCore P(Success) per epic for AIMarketScanner."""
    try:
        from alpha.micro_sniper_ml import sniper_ml_desk_payload

        return sniper_ml_desk_payload()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "threshold": 0.68,
            "by_epic": {},
            "latest": {},
        }


@router.get("/api/position_manager/status")
def api_position_manager_status() -> dict[str, Any]:
    """In-process open-position supervisor — last tick, actions, coverage."""
    from runtime.open_position_manager import snapshot as mgr_snap

    snap = mgr_snap()
    try:
        from runtime.micro_gbp_exit import snapshot as gbp_snap
        from runtime.virtual_stop_loss import virtual_stop_snapshot
        from runtime.dynamic_limit_engine import snapshot as dyn_snap

        snap["gbp_tracks"] = len((gbp_snap().get("tracks") or {}))
        snap["virtual_stops"] = len((virtual_stop_snapshot().get("positions") or []))
        snap["dynamic_limits"] = len((dyn_snap().get("tracks") or {}))
    except Exception:
        pass
    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            s = sync.snapshot()
            snap["sync_open"] = int(getattr(s, "total_open", 0) or 0)
            snap["sync_status"] = str(getattr(s, "sync_status", "") or "")
    except Exception:
        pass
    return {"ok": True, **snap}


@router.get("/api/trade_support/status")
def api_trade_support_status() -> dict[str, Any]:
    """Always-on open-trade supervisor — last cycle, broker book, managed actions.

    Reads the status file written by ``runtime.trade_support_wrapper`` (an
    independent process using IG REST as source of truth), so it stays accurate
    even when the in-process manager cache is stale or dead.
    """
    import json as _json
    import time as _time

    from system.paths import data_dir, legacy_src_data_dir

    candidates = [
        data_dir() / "trade_support_status.json",
        legacy_src_data_dir() / "trade_support_status.json",
    ]
    best: dict[str, Any] | None = None
    best_ts = -1.0
    for path in candidates:
        if not path.is_file():
            continue
        try:
            status = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ts = float(status.get("ts") or 0)
        if ts >= best_ts:
            best_ts = ts
            best = status
    if best is None:
        return {"ok": False, "running": False, "error": "no_status_file"}
    age = _time.time() - best_ts
    best["status_age_sec"] = round(age, 1)
    best["running"] = age < 60.0
    best["ok"] = True
    # SoT honesty: never report broker_open=0 when last-good snapshot has opens.
    try:
        from runtime import broker_snapshot

        snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
        snap_n = int(snap.get("count") or len(snap.get("positions") or []))
        status_n = int(best.get("broker_open") or 0)
        best["snapshot_open"] = snap_n
        if status_n == 0 and snap_n > 0:
            best["broker_open"] = snap_n
            best["sot_overlay"] = True
            best["source"] = f"{best.get('source') or 'status'}|sot_overlay_snapshot"
        # Stale status with opens → nudge heal via desk_support contract field.
        if age > 90.0 and snap_n > 0:
            best["heal_recommended"] = True
            best["running"] = False
    except Exception:
        pass
    return best


def _position_manager_tick_sync() -> dict[str, Any]:
    from runtime.open_position_manager import (
        ensure_open_position_manager,
        run_management_tick,
    )
    from system.config_loader import get_config
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import get_shared_rest_client

    cfg = get_config()
    rest = None
    cred = try_load_credentials()
    if cred.ok and cred.credentials:
        rest = get_shared_rest_client(cred.credentials)
    ensure_open_position_manager(rest, cfg=cfg)
    return run_management_tick(rest, cfg, execute=True)


@router.post("/api/position_manager/tick")
async def api_position_manager_tick() -> dict[str, Any]:
    """Force one in-process assess + manage cycle (Trading Desk supervision)."""
    import asyncio

    try:
        return await _run_dashboard_sync(_position_manager_tick_sync, timeout=12.0)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "tick_timeout"}


@router.get("/api/trading_desk/liveness")
async def api_trading_desk_liveness() -> dict[str, Any]:
    """Trading Desk AI connection health — non-blocking."""
    from runtime.trading_desk_liveness import evaluate_liveness

    return await _run_dashboard_sync(evaluate_liveness, timeout=2.0)


@router.post("/api/trading_desk/recover")
async def api_trading_desk_recover() -> dict[str, Any]:
    """Trigger failsafe recovery (sync nudge, manager tick, risk stack)."""
    from runtime.trading_desk_liveness import run_recovery_tick

    return await _run_dashboard_sync(lambda: run_recovery_tick(force=True), timeout=15.0)


@router.get("/api/strategy/improvement")
async def api_strategy_improvement() -> dict[str, Any]:
    """Rolling WR/PnL and epoch deltas as strategy shifts."""
    from runtime.strategy_improvement_tracker import snapshot

    return await _run_dashboard_sync(snapshot, timeout=2.0)


@router.get("/api/strategy/intraday_slots")
async def api_strategy_intraday_slots() -> dict[str, Any]:
    """BST intraday slot WR/PnL with per-slot improvement vs prior epoch/day."""
    from runtime.intraday_slot_tracker import snapshot

    return await _run_dashboard_sync(snapshot, timeout=2.0)


@router.get("/api/strategy/profit_tiers")
async def api_strategy_profit_tiers() -> dict[str, Any]:
    """% profit tier assessment by market — WR/PnL per tier bucket for ML review."""
    from execution.profit_pct_tiers import assess_profit_tier_strategy, build_pct_tiers
    from runtime.strategy_improvement_tracker import list_managed_closes
    from system.config_loader import get_config

    def _body() -> dict[str, Any]:
        cfg = get_config()
        closes = list_managed_closes(limit=200)
        assessment = assess_profit_tier_strategy(closes, cfg=cfg)
        ladders: dict[str, Any] = {}
        for epic in ("IX.D.DOW.IFM.IP", "IX.D.NIKKEI.IFM.IP"):
            try:
                from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

                _, _, prof = resolve_micro_tp_sl_for_epic(epic, 0.5, cfg)
                target = float(prof.risk_per_trade_gbp) * float(prof.target_r_multiple)
                tiers = build_pct_tiers(epic=epic, target_gbp=target, cfg=cfg)
                ladders[epic] = [
                    {
                        "pct": t.pct,
                        "peak_min_gbp": t.peak_min_gbp,
                        "bank_floor_gbp": t.bank_floor_gbp,
                        "label": t.label,
                    }
                    for t in tiers
                ]
            except Exception:
                ladders[epic] = []
        return {
            "ok": True,
            "close_count": len(closes),
            "profit_tier_assessment": assessment,
            "tier_ladders_gbp": ladders,
            "closes": closes,
        }

    return await _run_dashboard_sync(_body, timeout=3.0)


@router.get("/api/trade_events")
def api_trade_events(limit: int = 50) -> JSONResponse:
    """Typed trade/lifecycle events for trading path panel."""
    from api.trade_state_api import api_trade_events_json

    return api_trade_events_json(limit=min(max(limit, 1), 200))


@router.get("/api/rotation_state")
def api_rotation_state() -> JSONResponse:
    """Alias for rotation_status with history."""
    from api.trade_state_api import api_rotation_state_json

    return api_rotation_state_json()


@router.get("/api/data_feed_state")
def api_data_feed_state() -> JSONResponse:
    """Primary/backup feed health — Yahoo/Finnhub/Alpha first-past-the-post."""
    from system.feeds.data_feed_orchestrator import get_data_feed_state

    return JSONResponse(content=get_data_feed_state())


@router.get("/api/ig_budget_state")
def api_ig_budget_state() -> JSONResponse:
    """IG REST rate budget + cooldown state for cockpit guard banner."""
    from system.ig_budget_monitor import ig_budget_snapshot

    return JSONResponse(content=ig_budget_snapshot())


@router.get("/api/iron_cage_status")
def api_iron_cage_status() -> JSONResponse:
    """Iron Cage readiness contract — gates, feeds, execution, IG budget."""
    from system.iron_cage_readiness import fast_iron_cage_status_snapshot

    return JSONResponse(content=fast_iron_cage_status_snapshot())


@router.get("/api/iron_gauge")
async def api_iron_gauge() -> JSONResponse:
    """Unified startup cage — phases, tier, recovery log, trade contract."""
    from system.boot.iron_gauge import get_iron_gauge_snapshot

    return JSONResponse(content=get_iron_gauge_snapshot())


@router.get("/api/regime_state")
def api_regime_state() -> JSONResponse:
    """Markov regime switching state — ADX/ATR/spread over 1440m window."""
    from system.regime_state import get_regime_state_snapshot

    return JSONResponse(content=get_regime_state_snapshot())


@router.get("/api/risk_state")
def api_risk_state() -> JSONResponse:
    """Volatility-adaptive risk + circuit breaker telemetry."""
    from system.risk_state import get_risk_state_snapshot

    return JSONResponse(content=get_risk_state_snapshot())


@router.get("/api/tuner_state")
def api_tuner_state() -> JSONResponse:
    """Autonomous parameter tuner — regime matrix, metrics, optimization history."""
    from runtime.parameter_tuner import get_tuner_state_snapshot

    return JSONResponse(content=get_tuner_state_snapshot())


@router.get("/api/exploration_state")
def api_exploration_state() -> JSONResponse:
    """Multi-market portfolio exploration — universe, margin, correlation tree."""
    from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

    return JSONResponse(content=get_exploration_state_snapshot())


@router.get("/api/guardian_status")
def api_guardian_status() -> JSONResponse:
    """Chaos guardian — token buckets, reconnect history, state sync, packet health."""
    from system.chaos_guardian import get_guardian_status_snapshot

    return JSONResponse(content=get_guardian_status_snapshot())


@router.get("/api/orchestrator_state")
def api_orchestrator_state() -> JSONResponse:
    """Master orchestrator — warmup logs, strategy matrix, scoreboard, active loops."""
    from runtime.master_orchestrator import get_orchestrator_state_snapshot

    return JSONResponse(content=get_orchestrator_state_snapshot())


@router.get("/api/reporting_status")
def api_reporting_status() -> JSONResponse:
    """Alert matrix — webhook status, queue depth, recent broadcasts."""
    from system.alert_reporting_matrix import get_reporting_status_snapshot

    return JSONResponse(content=get_reporting_status_snapshot())


@router.get("/api/macro_steering")
def api_macro_steering() -> JSONResponse:
    """Macro steering surface — sentiment ROC, news countdown, 48-bar shadow-walk."""
    from cockpit.sre_snapshots import get_macro_steering_snapshot

    return JSONResponse(content=get_macro_steering_snapshot())


@router.get("/api/ai_diagnostics")
def api_ai_diagnostics() -> JSONResponse:
    """Autonomic healer + ML cognitive diagnostics — uncompressed JSON snapshot."""
    from system.autonomic_healer import get_ai_diagnostics_snapshot

    return JSONResponse(content=get_ai_diagnostics_snapshot())


@router.get("/api/latency_trace")
def api_latency_trace() -> JSONResponse:
    """Tick-to-trade latency ring buffer summary."""
    from system.latency_trace import get_latency_trace_snapshot

    return JSONResponse(content=get_latency_trace_snapshot())


@router.get("/api/reconciliation_state")
def api_reconciliation_state() -> JSONResponse:
    """Broker vs internal position reconciliation status."""
    from system.broker_reconciliation_daemon import get_reconciliation_snapshot

    return JSONResponse(content=get_reconciliation_snapshot())


@router.get("/api/multimarket_eval")
def api_multimarket_eval() -> JSONResponse:
    """Per-market evaluation snapshot — signals, lifecycle, P&L, feed health."""
    from analytics.multimarket_eval import get_multimarket_eval_snapshot

    return JSONResponse(content=get_multimarket_eval_snapshot())


@router.get("/api/trade_quality")
def api_trade_quality() -> JSONResponse:
    """Trade quality metrics — acceptance, slippage, risk vs P&L."""
    from analytics.trade_quality import get_trade_quality_snapshot

    return JSONResponse(content=get_trade_quality_snapshot())


@router.get("/api/tuning_params")
def api_tuning_params() -> JSONResponse:
    """Runtime tuning parameters (read-only overlay merge)."""
    from analytics.tuning_params import get_tuning_params

    return JSONResponse(content=get_tuning_params())


@router.post("/api/tuning_update")
async def api_tuning_update(request: Request) -> JSONResponse:
    """Apply validated tuning overlay — never overrides iron cage hard limits."""
    from analytics.tuning_params import apply_tuning_update

    try:
        body = await request.json()
    except Exception:
        body = {}
    params = body.get("params") if isinstance(body, dict) else body
    if not isinstance(params, dict):
        params = body if isinstance(body, dict) else {}
    result = apply_tuning_update(params, source="cockpit_api")
    status = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status, content=result)


@router.get("/api/health_light")
async def api_health_light() -> JSONResponse:
    """Lightweight O(1) system health — <5ms, no external calls."""
    import time

    from api.endpoint_profiler import record_request
    from api.health_light import get_health_light_response

    t0 = time.perf_counter()
    body = get_health_light_response()
    record_request("health_light", (time.perf_counter() - t0) * 1000.0)
    return JSONResponse(content=body)


@router.get("/api/gui_status")
def api_gui_status() -> dict[str, Any]:
    """Read-only GUI polling — session identity + pipeline health indicators."""
    import time

    from api.endpoint_profiler import record_request
    from api.gui_status import get_gui_status_cached
    from datetime import datetime, timezone

    t0 = time.perf_counter()
    body = get_gui_status_cached()
    body["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    record_request("gui_status", (time.perf_counter() - t0) * 1000.0)
    return body


@router.get("/api/readiness/profile")
def api_readiness_profile() -> dict[str, Any]:
    """Endpoint timing aggregates — for load profiling and slow-path diagnosis."""
    from api.endpoint_profiler import timing_summary
    from api.readiness_snapshot import _META

    return {
        "timings": timing_summary(),
        "snapshot_meta": dict(_META),
        "latency_budget_ms": 200,
    }


@router.get("/api/diagnostics")
def api_system_diagnostics() -> dict[str, Any]:
    """Unified organism view — routing, risk, feeds, execution, gates."""
    import time

    from api.endpoint_profiler import record_request
    from api.system_diagnostics import build_system_diagnostics

    t0 = time.perf_counter()
    body = build_system_diagnostics()
    record_request("diagnostics", (time.perf_counter() - t0) * 1000.0)
    return body


@router.get("/api/time")
def get_agent_time() -> dict[str, Any]:
    """Agent-resolved Europe/London clock for dashboard header (display only)."""
    from api.agent_time import get_agent_time_payload

    return get_agent_time_payload()


@router.get("/api/startup/status")
def get_startup_status() -> dict[str, Any]:
    """Real-time boot progress — polled by StartupSplash and dashboard init banner."""
    from system.boot_metrics import get_boot_metrics
    from system.system_state import get_system_state

    boot_metrics = get_boot_metrics()
    system_state = get_system_state().snapshot()
    from api.restriction_diagnostics import enrich_restrictions_payload

    ready = bool(system_state.get("ready")) and bool(boot_metrics.get("ready"))
    if str(boot_metrics.get("stage") or "") == "warming":
        ready = False

    return enrich_restrictions_payload(
        {
            "boot_metrics": boot_metrics,
            "system_state": system_state,
            "ready": ready,
            "background_verify": system_state.get("background_verify") or {},
        }
    )


@router.get("/state")
def state() -> dict[str, Any]:
    """Full dashboard snapshot — same schema as WebSocket tick messages."""
    from api.agent_control import enrich_tick_runtime

    tick = enrich_tick_runtime(get_tick())
    tick["trading_paused"] = is_paused()
    tick["trading_loops_running"] = is_trading_running()
    return tick


@router.get("/api/splash")
def api_splash_state() -> dict[str, Any]:
    return read_version_state()


@router.post("/api/splash/dismiss")
def api_splash_dismiss() -> dict[str, Any]:
    return dismiss_splash()


@router.post("/api/start")
def api_start() -> dict[str, Any]:
    result = start_trading()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "start failed"))
    return result


@router.post("/api/stop")
def api_stop() -> dict[str, Any]:
    result = stop_trading()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "stop failed"))
    return result


@router.post("/api/operational/unblock")
def api_operational_unblock() -> dict[str, Any]:
    """Clear cockpit emergency override, manual stop, and execution blocks."""
    from cockpit.emergency import clear_emergency_cockpit_override
    from api.v31_telemetry import resolve_risk_tracking_fields

    result = clear_emergency_cockpit_override(resume_trading=True)
    result.update(resolve_risk_tracking_fields())
    return result


@router.post("/api/emergency_stop")
def api_emergency_stop() -> dict[str, Any]:
    result = run_emergency_stop()
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


@router.post("/api/v1/emergency/kill")
def api_emergency_kill_v1() -> JSONResponse:
    """Instant kill — stop loops, cancel working orders, flatten all broker opens.

    Bypasses normal trading-loop cadence; uses exit_execution_gate for closes.
    """
    from api.emergency_kill import run_emergency_kill

    result = run_emergency_kill(source="api_v1")
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


@router.post("/api/emergency/kill")
def api_emergency_kill_alias() -> JSONResponse:
    """Alias for /api/v1/emergency/kill."""
    return api_emergency_kill_v1()


@router.get("/api/trades")
def api_trades(limit: int = 10) -> dict[str, Any]:
    trades = get_closed_trades(limit=min(100, max(1, limit)))
    points_total = sum(float(t.get("points_score") or 0) for t in trades)
    return {"trades": trades, "points_total": points_total}


@router.get("/api/trades/shadow")
def api_shadow_trades(limit: int = 20) -> dict[str, Any]:
    trades = get_shadow_closed_trades(limit=min(200, max(1, limit)))
    return {"trades": trades, "count": len(trades)}


@router.post("/api/trades/reconcile")
def api_reconcile_trades() -> dict[str, Any]:
    """Manually trigger an immediate trade reconciliation against IG history."""
    from runtime.ig_transaction_sync import get_transaction_sync_instance

    sync = get_transaction_sync_instance()
    if sync is None:
        return JSONResponse(
            {"ok": False, "error": "Transaction sync not running (agent offline?)"},
            status_code=503,
        )
    scheduled = sync.request_sync(force=True, reason="manual-reconcile")
    return {"ok": True, "scheduled": scheduled}


@router.get("/api/signals")
def api_signals(limit: int = 50) -> dict[str, Any]:
    return {"signals": get_signal_log(limit=min(100, max(1, limit)))}


@router.post("/api/admin/reset-correlation-guard")
def api_admin_reset_correlation_guard() -> JSONResponse:
    """Reset session BUY/SELL entry counters to 0/5 (opens execution gateway)."""
    from execution.correlation_guard import reset_session, snapshot
    from system.engine_log import log_engine

    reset_session()
    snap = snapshot()
    log_engine(f"admin: correlation guard reset — buy={snap.get('buy')} sell={snap.get('sell')}")
    return JSONResponse({"ok": True, "snapshot": snap})


@router.post("/api/admin/unlimited-trading")
def api_admin_unlimited_trading() -> JSONResponse:
    """DEMO soak — clear session trade caps and correlation counters."""
    from execution.correlation_guard import force_purge_session_correlation_counters, snapshot
    from system.demo_execution_plane import arm_demo_unlimited_trading_session
    from system.engine_log import log_engine

    arm_demo_unlimited_trading_session(clear_counts=True)
    force_purge_session_correlation_counters(reason="operator_unlimited_trading")
    snap = snapshot()
    log_engine(
        "admin: unlimited trading armed — session caps cleared "
        f"buy={snap.get('buy')} sell={snap.get('sell')}"
    )
    return JSONResponse({"ok": True, "unlimited": True, "correlation_guard": snap})


@router.post("/api/sim/tick")
async def api_sim_tick(request: Request) -> JSONResponse:
    """Sandbox simulation — publish a synthetic tick into the live market data hub.

    Gated behind ``sandbox_mode_enabled`` in config; rejected outright in production.
    """
    import time

    from system.config_loader import get_config
    from system.market_data_hub import get_market_data_hub

    cfg = get_config()
    if not getattr(cfg, "sandbox_mode_enabled", False):
        return JSONResponse(
            {"ok": False, "error": "sandbox_mode_enabled is false — endpoint disabled"},
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    epic = str(body.get("epic") or "").strip()
    bid = float(body.get("bid") or 0)
    offer = float(body.get("offer") or 0)
    if not epic or bid <= 0 or offer <= 0:
        return JSONResponse(
            {"ok": False, "error": "epic, bid, offer required (positive floats)"},
            status_code=400,
        )

    hub = get_market_data_hub()
    snap = hub.publish(epic, bid, offer, source="sandbox", quote_time=time.time())
    return JSONResponse({
        "ok": True,
        "epic": epic,
        "bid": bid,
        "offer": offer,
        "accepted": snap is not None,
    })


@router.post("/api/signal/inject")
async def api_signal_inject(request: Request) -> JSONResponse:
    """Inject a synthetic operator signal into the gate stack for one tick."""
    from execution.signal_injection import enqueue_injection, pending_injections
    from runtime.market_orchestrator import MarketOrchestrator
    from system.engine_log import log_engine

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    epic = str(body.get("epic") or "").strip()
    direction = str(body.get("direction") or "").strip().upper()

    if not epic:
        return JSONResponse({"ok": False, "error": "epic is required"}, status_code=400)
    if direction not in ("BUY", "SELL"):
        return JSONResponse(
            {"ok": False, "error": "direction must be BUY or SELL"}, status_code=400
        )

    active = MarketOrchestrator.get_global_active_epics()
    if active and epic not in active:
        return JSONResponse(
            {"ok": False, "error": f"epic '{epic}' not in active rotation", "active": active},
            status_code=400,
        )

    pending = pending_injections()
    if epic in pending:
        return JSONResponse(
            {"ok": False, "error": f"injection already queued for {epic}", "pending": pending[epic]},
            status_code=409,
        )

    entry = enqueue_injection(epic, direction)
    log_engine(f"signal_inject: operator queued {direction} for {epic}")
    return JSONResponse({"ok": True, "status": "queued", **entry})


@router.get("/api/signal/inject/pending")
def api_signal_inject_pending() -> JSONResponse:
    """Current pending signal injections."""
    from execution.signal_injection import pending_injections

    return JSONResponse({"ok": True, "pending": pending_injections()})


@router.post("/api/admin/force_snapshot_sync")
async def api_admin_force_snapshot_sync() -> JSONResponse:
    """Invalidate stale GUI book: mirror broker_snapshot + re-arm in-memory GBP tracks.

    Use after offline reconcile repaired disk/DB but the agent PID still serves
    ``gbp_track_fallback`` with ``entry=0``. No process restart required once this
    route is loaded in the running API process.
    """
    from system.engine_log import log_engine

    try:
        from runtime.broker_snapshot import force_snapshot_sync

        result = await _run_dashboard_sync(force_snapshot_sync, timeout=5.0)
        log_engine(
            "admin/force_snapshot_sync: "
            f"ok={result.get('ok')} rearmed={result.get('rearmed')} "
            f"actions={result.get('actions')}"
        )
        return JSONResponse(result)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "force_snapshot_sync_timeout"},
            status_code=504,
        )
    except Exception as exc:
        log_engine(f"admin/force_snapshot_sync failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/admin/force-close")
async def api_admin_force_close(request: Request) -> JSONResponse:
    """Admin override — immediate MARKET close for one epic (bypasses trailing delays)."""
    from system.engine_log import log_engine
    from trading.manual_intervention import force_terminate_position

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    epic = str(body.get("epic") or "").strip()
    if not epic:
        raise HTTPException(status_code=400, detail="epic required in JSON body")

    try:
        result = force_terminate_position(epic)
        return JSONResponse(result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_engine(f"admin/force-close failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/admin/force-breakeven")
async def api_admin_force_breakeven(request: Request) -> JSONResponse:
    """Admin override — move stop to entry immediately (bypasses trailing thresholds)."""
    from system.engine_log import log_engine
    from trading.manual_intervention import force_breakeven_now

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    epic = str(body.get("epic") or "").strip()
    if not epic:
        raise HTTPException(status_code=400, detail="epic required in JSON body")

    try:
        result = force_breakeven_now(epic)
        return JSONResponse(result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_engine(f"admin/force-breakeven failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/admin/reset-points")
async def api_admin_reset_points(request: Request) -> JSONResponse:
    """Admin override — reset points engine cumulative to HEALTHY."""
    from system.engine_log import log_engine
    from trading.points_engine import PointsEngine

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400, detail="confirm: true required in JSON body"
        )

    try:
        engine = PointsEngine()
        previous, new_state = engine.admin_reset_cumulative()
        log_engine(
            f"[POINTS RESET] Cumulative reset to 0 — operator action "
            f"(previous={previous:.2f})"
        )
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert("[ADMIN] Points engine reset by operator")
        except Exception as exc:
            log_engine(
                f"telegram points-reset alert failed: {type(exc).__name__}: {exc}"
            )
        return JSONResponse(
            {
                "success": True,
                "previous_cumulative": previous,
                "new_state": new_state,
            }
        )
    except Exception as exc:
        log_engine(f"admin/reset-points failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/admin/toggle-rotation-filter")
def api_admin_toggle_rotation_filter() -> dict[str, Any]:
    """Flip enforce_top3_rotation_filter and persist to the primary config file."""
    from runtime.market_orchestrator import MarketOrchestrator
    from system.config_loader import get_config, update_config_values
    from system.engine_log import log_engine

    try:
        cfg = get_config()
        current = bool(cfg.get("enforce_top3_rotation_filter", True))
        new_value = not current
        updated = update_config_values(enforce_top3_rotation_filter=new_value)
        try:
            # Push the updated config into active loops immediately.
            MarketOrchestrator.hot_reload_config(updated)
        except Exception:
            pass
        log_engine(
            "admin/toggle-rotation-filter: "
            f"enforce_top3_rotation_filter={new_value}"
        )
        return {
            "success": True,
            "enforce_top3_rotation_filter": bool(
                updated.get("enforce_top3_rotation_filter", new_value)
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/admin/risk-status")
def api_admin_risk_status() -> dict[str, Any]:
    """Admin view — drawdown shield latch, closed-day loss, and daily loss gate."""
    from system.engine_log import log_engine
    from trading.manual_intervention import risk_status

    try:
        return risk_status()
    except Exception as exc:
        log_engine(f"admin/risk-status failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/admin/flush-portfolio-risk")
def api_admin_flush_portfolio_risk() -> dict[str, Any]:
    """Reconcile in-memory portfolio risk against open trades (stale reservation recovery)."""
    from data.learning_store import LearningStore
    from execution.portfolio_hooks import rehydrate_portfolio_from_store
    from system.config_loader import get_config
    from system.engine_log import log_engine
    from system.portfolio_envelope import rehydrate, snapshot

    try:
        before = snapshot()
        cfg = get_config()
        store = LearningStore(str(cfg.learning_db))
        try:
            rehydrate_portfolio_from_store(store, cfg=cfg)
        except Exception:
            rehydrate(concurrent_risk_gbp=0.0, daily_deployed_gbp=0.0)
        after = snapshot()
        log_engine(
            "admin/flush-portfolio-risk: "
            f"concurrent £{before.get('concurrent_risk_gbp', 0):.0f} → "
            f"£{after.get('concurrent_risk_gbp', 0):.0f}"
        )
        return {
            "ok": True,
            "before": before,
            "after": after,
        }
    except Exception as exc:
        log_engine(
            f"admin/flush-portfolio-risk failed: {type(exc).__name__}: {exc}"
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/admin/export-shadow", response_model=None)
def api_admin_export_shadow(download: bool = True) -> Response | dict[str, Any]:
    """
    Admin audit export — read-only dump of shadow_training_registry as CSV.

    ?download=false returns JSON summary + inline csv_text for GUI preview.
    """
    from system.data_exporter import export_shadow_registry_to_csv
    from system.engine_log import log_engine

    try:
        result = export_shadow_registry_to_csv()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        log_engine(f"admin/export-shadow failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="shadow registry read failed") from exc

    if not download:
        return result

    summary = result.get("summary") or {}
    stamp = str(summary.get("exported_at") or "export").replace(":", "").replace("-", "")
    filename = f"shadow_registry_{stamp}.csv"
    return Response(
        content=result.get("csv_text") or "",
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Shadow-Row-Count": str(summary.get("row_count", 0)),
            "X-Shadow-Win-Rate": str(summary.get("overall_win_rate", 0)),
        },
    )


@router.get("/api/system")
def api_system() -> dict[str, Any]:
    info = get_system_info()
    try:
        from trading.strictness_resolver import strictness_payload

        info["trading_strictness"] = strictness_payload()
    except Exception:
        pass
    return info


@router.get("/api/roadmap/progress")
def api_roadmap_progress(days: int = 7) -> dict[str, Any]:
    """Gap audit checklist — certification, edge, coverage, daily flow."""
    from api.roadmap_progress import build_roadmap_progress

    return build_roadmap_progress(
        history_days=min(30, max(1, days)),
        write_snapshot=True,
    )


@router.get("/api/daily-digest")
def api_daily_digest() -> dict[str, Any]:
    """Daily operator briefing markdown for the dashboard popup."""
    from api.daily_digest import load_daily_digest

    try:
        return load_daily_digest(regenerate_if_stale=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"daily digest unavailable: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/api/learning-health")
def api_learning_health(refresh_registry: bool = False) -> dict[str, Any]:
    """Learning pipeline status — ML, registry, agent P&L, policy."""
    from system.learning_health import build_learning_health_report

    try:
        return build_learning_health_report(refresh_registry=refresh_registry)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"learning health unavailable: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/api/stats/edge-analysis")
def api_edge_analysis() -> dict[str, Any]:
    """Read-only edge statistics for STATS dashboard tab."""
    from api.edge_analysis import get_edge_analysis_payload

    try:
        return get_edge_analysis_payload()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"edge analysis unavailable: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/api/gates/attribution")
def api_gates_attribution(days: int = 7, rotated: bool = True) -> dict[str, Any]:
    """Ranked gate blockers from engine.log WAIT lines."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from gate_attribution_report import rollup_gate_blocks

    log_path = root / "src" / "data" / "logs" / "engine.log"
    return rollup_gate_blocks(
        log_path=log_path,
        days=min(30, max(1, days)),
        include_rotated=rotated,
    )


@router.get("/api/gates/binding-histogram")
def api_gates_binding_histogram(days: int = 7) -> dict[str, Any]:
    """Gate blockers + SUBMIT_TRUTH binding histogram for session audit."""
    from api.gate_binding import build_gate_binding_report

    return build_gate_binding_report(days=min(30, max(1, days)))


@router.get("/api/config/strictness")
def api_get_strictness() -> dict[str, Any]:
    from trading.strictness_resolver import strictness_payload

    return {"ok": True, **strictness_payload()}


@router.post("/api/config/strictness")
async def api_set_strictness(request: Request) -> JSONResponse:
    """Manual strictness overrides are deprecated — velocity regime is automated."""
    from system.engine_log import log_engine
    from trading.strictness_resolver import strictness_payload

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    requested = body.get("profile")
    if requested:
        log_engine(
            "api/config/strictness: manual profile ignored — "
            f"requested={requested!r}; strictness is velocity-driven per market loop"
        )

    payload = strictness_payload()
    return JSONResponse(
        {
            "ok": True,
            "ignored": True,
            "message": "Manual strictness overrides are disabled; profile is velocity-driven.",
            **payload,
        }
    )


@router.get("/api/replay/summary")
def api_replay_summary() -> dict[str, Any]:
    return replay_summary()


@router.get("/api/shadow/today")
def api_shadow_today() -> dict[str, Any]:
    return shadow_today()


@router.get("/api/v26/profit")
def api_v26_profit() -> dict[str, Any]:
    """v26 PROFIT tab — read-only expectancy + shadow strategy attribution."""
    from api.v26_profit import build_profit_payload

    return build_profit_payload()


@router.get("/api/v26/cert")
def api_v26_cert() -> dict[str, Any]:
    """v26 CERT tab — L0–L5 certification ladder."""
    from api.v26_cert import build_cert_payload

    return build_cert_payload()


@router.get("/api/v30/cert")
def api_v30_cert() -> dict[str, Any]:
    """v30 CERT tab — ML certification ladder from v30 data lake."""
    from api.v30_cert import build_v30_cert_payload

    return build_v30_cert_payload()


@router.get("/api/v31/broker/ready")
async def api_v31_broker_ready() -> dict[str, Any]:
    """IG Trading Ready — auth, stream, order valve, ledger sync."""
    from api.v31_broker_ready import get_dashboard_broker_ready

    try:
        return await _run_dashboard_sync(get_dashboard_broker_ready, timeout=None)
    except Exception:
        return {
            "ok": True,
            "ig_trading_ready": False,
            "broker_auth_valid": False,
            "socket_stream_active": False,
            "order_execution_ready": False,
            "ledger_synced": False,
            "display": {
                "authenticated": "FAILED",
                "data_stream": "WARMING",
                "order_valve": "SUPPRESSED",
                "ledger_sync": "DRIFTING",
            },
            "details": {"stream": "broker_ready_error"},
        }


@router.get("/api/v31/failover")
async def api_v31_failover() -> dict[str, Any]:
    """In-flight session failover state — forex lock + ML sovereignty."""
    from api.v31_telemetry import resolve_ml_alpha_weight
    from runtime.dual_core_execution import get_failover_state, get_stacked_asset_channels

    def _build() -> dict[str, Any]:
        state = get_failover_state()
        return {
            "ok": True,
            **state,
            "stacked_asset_channels": get_stacked_asset_channels(),
            "ml_alpha_weight": resolve_ml_alpha_weight(),
        }

    return await asyncio.to_thread(_build)


@router.get("/api/v31/telemetry")
async def api_v31_telemetry() -> dict[str, Any]:
    """Core night-matrix quotes, IG account capital, transport RTT, gate stack."""
    from api.v31_telemetry import get_dashboard_telemetry

    try:
        return await _run_dashboard_sync(get_dashboard_telemetry, timeout=None)
    except Exception:
        try:
            from runtime.ledger_hydration_core import get_cached_ledger_rows, ledger_hydration_state

            ledger = ledger_hydration_state()
            return {
                "ok": False,
                "degraded": True,
                "error": "telemetry_build_failed",
                "ts": time.time(),
                "active_positions": get_cached_ledger_rows(),
                **ledger,
            }
        except Exception:
            return {
                "ok": False,
                "degraded": True,
                "error": "telemetry_build_failed",
                "ts": time.time(),
                "active_positions": [],
            }


@router.get("/api/v31/gate-stack")
async def api_v31_gate_stack() -> dict[str, Any]:
    """Core B live gate boolean matrix — stream / macro / risk netting."""
    from runtime.dual_core_execution import resolve_core_b_gate_stack

    return await asyncio.to_thread(resolve_core_b_gate_stack)


@router.get("/api/v31/positions")
async def api_v31_positions() -> dict[str, Any]:
    """Live open contracts from IgPositionSync."""
    from api.v31_telemetry import build_v31_positions

    return await asyncio.to_thread(build_v31_positions)


@router.get("/api/v31/history")
async def api_v31_history(limit: int = 10) -> dict[str, Any]:
    """Latest closed trade outcomes from triage_v31.db."""
    from api.v31_telemetry import build_v31_history

    try:
        return await _run_dashboard_sync(build_v31_history, limit=limit, timeout=2.0)
    except asyncio.TimeoutError:
        return {"ok": False, "degraded": True, "rows": [], "count": 0}


@router.post("/api/v31/orders/fulfill", status_code=202)
async def api_v31_orders_fulfill(request: Request) -> dict[str, Any]:
    """v31 production-plane synthetic breakout intake → async 202 + background IG REST."""
    from api.v31_orders import accept_v31_breakout_order

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    boot_ctx = getattr(request.app.state, "boot_context", None)
    try:
        return await accept_v31_breakout_order(body, boot_context=boot_ctx)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        from ig_api.exceptions import IGAPIError, IGOrderError

        if isinstance(exc, (IGOrderError, IGAPIError)):
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise


@router.get("/api/trades/triage-ledger")
def api_trades_triage_ledger(limit: int = 50) -> dict[str, Any]:
    """Authoritative fill ledger from triage_v30.db."""
    from api.triage_ledger import fetch_triage_ledger

    return fetch_triage_ledger(limit=min(200, max(1, limit)))


@router.get("/api/stats/triage")
def api_stats_triage() -> dict[str, Any]:
    """Rolling Sharpe, slippage, spread premium from triage_v30.db."""
    from api.triage_ledger import fetch_triage_stats

    return fetch_triage_stats()


@router.get("/api/v27/sentinel/diagnostics")
def api_v27_sentinel_diagnostics(limit: int = 80) -> dict[str, Any]:
    """v27 Autonomous Sentinel — terminal diagnostic stream payload."""
    from api.v27_sentinel import build_sentinel_diagnostics

    return build_sentinel_diagnostics(limit=min(200, max(1, limit)))


@router.post("/api/v27/sentinel/approve")
async def api_v27_sentinel_approve(request: Request) -> dict[str, Any]:
    """Human approval of strategy proposal → Operational AI e2e validation (§19)."""
    from api.v27_sentinel import approve_strategy_proposal

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    proposal_id = str(body.get("proposal_id") or body.get("id") or "").strip()
    if not proposal_id:
        raise HTTPException(status_code=400, detail="proposal_id required")
    return approve_strategy_proposal(proposal_id)


@router.get("/api/learning/status")
def api_learning_status() -> dict[str, Any]:
    return learning_status()


@router.get("/api/intelligence/dashboard")
def api_intelligence_dashboard() -> dict[str, Any]:
    """Glass cockpit intelligence plane — replay, shadow, learning, live microstructure."""
    return intelligence_dashboard()


@router.get("/api/shadow/brain")
def api_shadow_brain() -> dict[str, Any]:
    """Shadow Brain (:9199) — data health, gate funnel, live tolerance output."""
    from intelligence.shadow_brain_loop import brain_dashboard_payload

    return brain_dashboard_payload()


@router.get("/api/shadow/alpha-matrix")
def api_shadow_alpha_matrix() -> dict[str, Any]:
    """Shadow (:9199) — pre-baked alpha matrix compiler + lookup telemetry."""
    from intelligence.matrix_prebaker import alpha_matrix_dashboard_payload

    return alpha_matrix_dashboard_payload()


@router.get("/api/unified/performance")
def api_unified_performance() -> dict[str, Any]:
    """Unified engine — Thread A/B alignment, e2e latency, vector density."""
    from system.unified_engine import unified_performance_payload

    return unified_performance_payload()


@router.post("/api/cockpit/heal")
def api_cockpit_heal(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Feed + SHM self-heal — hard-reset feeds and re-publish cockpit segment."""
    from system.unified_fulfillment_cache import force_cockpit_feed_heal

    reason = str((payload or {}).get("reason") or "api")
    return force_cockpit_feed_heal(reason=reason)


@router.get("/api/unified/fulfillment")
def api_unified_fulfillment() -> JSONResponse:
    """Decoupled 500ms fulfillment snapshot — zero hot-path cost, no browser cache."""
    from system.unified_fulfillment_cache import get_fulfillment_payload

    baseline: dict[str, Any] = {
        "mode": "UNIFIED_FULFILLMENT",
        "updated_at": "",
        "refresh_ms": 500,
        "stages": [],
        "market_quotes": {},
        "market_quotes_list": [],
        "gate_diagnostics": {"by_epic": {}, "last": {}},
        "alpha_frontier_tracker": {"by_epic": {}, "last": {}, "ring": {}},
        "ticks_cached": 0,
        "all_ready": False,
        "traffic_light_hub": {},
    }
    try:
        payload = get_fulfillment_payload()
        if not isinstance(payload, dict):
            payload = baseline
        payload.setdefault("market_quotes", {})
        payload.setdefault("gate_diagnostics", {"by_epic": {}, "last": {}})
        if not isinstance(payload.get("gate_diagnostics"), dict):
            payload["gate_diagnostics"] = {"by_epic": {}, "last": {}}
        gd = payload["gate_diagnostics"]
        gd.setdefault("by_epic", {})
        gd.setdefault("last", {})
        content = json.loads(json.dumps(payload, default=str))
    except Exception as exc:
        from system.engine_log import log_engine

        log_engine(f"api/unified/fulfillment serializer fallback: {type(exc).__name__}: {exc}")
        baseline["error"] = f"{type(exc).__name__}: {exc}"
        content = baseline
    return JSONResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.post("/api/internal/ui-stress-render")
def api_internal_ui_stress_render() -> dict[str, Any]:
    """Arm temporary 50Hz Gold UI stress burst (5 min, no broker orders)."""
    from intelligence.telemetry_daemon import execute_ui_stress_test_render

    status = execute_ui_stress_test_render()
    return {"ok": True, **status}


@router.post("/api/internal/live-tolerance")
def api_internal_live_tolerance(payload: dict[str, Any]) -> dict[str, Any]:
    """Shadow dispatcher handoff — apply gate-floor adjustments on Live Vanguard."""
    from system.identity.live_tolerance_bridge import apply_tolerance_payload

    applied = apply_tolerance_payload(payload)
    return {"ok": applied, "applied": applied, "source": payload.get("source")}


@router.post("/api/replay/run")
def api_replay_run() -> dict[str, Any]:
    return run_replay_pipeline()


@router.post("/api/system/tests")
def api_system_tests() -> dict[str, Any]:
    return run_system_tests()


@router.post("/api/system/e2e")
def api_system_e2e() -> dict[str, Any]:
    """E2E execution check — mock pipeline + IG DEMO routing (no order)."""
    return run_e2e_execution_check()


@router.post("/api/safe-to-leave")
def api_safe_to_leave() -> dict[str, Any]:
    """Run overnight trust bundle (launchd + checks). Never shuts down the agent."""
    from system.engine_log import log_engine

    log_engine(
        "safe-to-leave: overnight bundle started "
        "(launchd supervision + trust checks, no shutdown)"
    )
    return run_safe_to_leave()


@router.get("/api/overnight/status")
def api_overnight_status() -> dict[str, Any]:
    """Launchd supervision + armed state (independent of Cursor)."""
    from system.overnight_supervision import overnight_supervision_summary

    return {"ok": True, **overnight_supervision_summary()}


@router.post("/api/close/{deal_id}")
def api_close_deal(deal_id: str) -> JSONResponse:
    """Manual position close — routes to IG close_position()."""
    try:
        result = close_deal(deal_id)
        status = str(result.get("status") or "CLOSED")
        return JSONResponse(
            {"ok": True, "deal_id": deal_id, "result": result, "status": status}
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"close failed: {type(e).__name__}: {e}",
        ) from e


@router.post("/api/flatten/all")
def api_flatten_all() -> JSONResponse:
    """Close all open positions via IG REST. Logs and Telegrams the action."""
    from system.engine_log import log_engine
    from system.telegram_notifier import get_telegram_notifier

    try:
        from system.config_loader import ConfigLoader
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated
        from system.paths import config_dir

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            raise RuntimeError(status.error or "credentials missing")

        from system.config_loader import load_active_config

        cfg = load_active_config(validate=False)
        rest = ensure_shared_authenticated(status.credentials)
        positions = rest.open_positions()
        closed = []
        errors = []
        for item in positions:
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            deal_id = str(pos.get("dealId") or "")
            epic = str(mkt.get("epic") or "")
            side = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            if not deal_id or size <= 0:
                continue
            # close_position(skip_lookup=True) inverts OPEN once — pass OPEN side.
            try:
                rest.close_position(
                    deal_id,
                    direction=side,
                    size=size,
                    epic=epic or None,
                    currency_code=cfg.currency_code,
                    verify=False,
                    budget_priority=True,
                    skip_lookup=True,
                    skip_confirm=True,
                )
                closed.append(deal_id)
                log_engine(f"flatten_all: closed {epic} deal={deal_id}")
            except Exception as e:
                errors.append(f"{deal_id}: {e}")
                log_engine(f"flatten_all error {deal_id}: {e}")

        try:
            from data.learning_store import LearningStore
            from execution.portfolio_hooks import rehydrate_portfolio_from_store

            store = LearningStore(str(cfg.learning_db))
            rehydrate_portfolio_from_store(store, cfg=cfg)
            log_engine("flatten_all: portfolio envelope rehydrated from store")
        except Exception as fe:
            log_engine(
                f"flatten_all: portfolio rehydrate failed (continuing): {fe}"
            )

        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send(
                f"🔴 FLATTEN ALL — {len(closed)} position(s) closed"
                + (f"\nErrors: {len(errors)}" if errors else "")
            )
        return JSONResponse(
            {
                "ok": True,
                "closed": closed,
                "errors": errors,
                "count": len(closed),
            }
        )
    except Exception as e:
        log_engine(f"flatten_all failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/flatten/{epic}")
def api_flatten_epic(epic: str) -> JSONResponse:
    """Close all positions for a specific epic via IG REST."""
    from system.engine_log import log_engine
    from system.telegram_notifier import get_telegram_notifier

    try:
        from system.config_loader import ConfigLoader
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated
        from system.paths import config_dir

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            raise RuntimeError(status.error or "credentials missing")

        from system.config_loader import load_active_config

        cfg = load_active_config(validate=False)
        rest = ensure_shared_authenticated(status.credentials)
        positions = rest.open_positions()
        closed = []
        for item in positions:
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            deal_id = str(pos.get("dealId") or "")
            pos_epic = str(mkt.get("epic") or "")
            if pos_epic != epic:
                continue
            side = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            if not deal_id or size <= 0:
                continue
            # close_position(skip_lookup=True) inverts OPEN once — pass OPEN side.
            rest.close_position(
                deal_id,
                direction=side,
                size=size,
                epic=epic,
                currency_code=cfg.currency_code,
                verify=False,
                budget_priority=True,
                skip_lookup=True,
                skip_confirm=True,
            )
            closed.append(deal_id)
            log_engine(f"flatten_epic: closed {epic} deal={deal_id}")

        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send(f"🔴 FLATTEN {epic} — {len(closed)} position(s) closed")
        return JSONResponse({"ok": True, "epic": epic, "closed": closed})
    except Exception as e:
        log_engine(f"flatten_epic {epic} failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/agent/stop")
def api_agent_stop() -> JSONResponse:
    """Flatten all positions then stop the trading loop. Telegrams the action."""
    from system.engine_log import log_engine
    from system.telegram_notifier import get_telegram_notifier

    try:
        # Flatten first — best-effort
        try:
            api_flatten_all()
        except Exception as fe:
            log_engine(f"agent/stop: flatten failed (continuing): {fe}")

        result = stop_trading()
        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send("🔴 IG Agent v25 stopped (via dashboard)")
        log_engine("agent/stop: trading loop stopped via API")
        return JSONResponse(
            {"ok": result.get("ok", False), "status": result.get("status")}
        )
    except Exception as e:
        log_engine(f"agent/stop failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/agent/restart")
def api_agent_restart() -> JSONResponse:
    """Flatten all → stop → start the trading loop."""
    from system.engine_log import log_engine
    from system.telegram_notifier import get_telegram_notifier

    try:
        try:
            api_flatten_all()
        except Exception as fe:
            log_engine(f"agent/restart: flatten failed (continuing): {fe}")

        stop_trading()
        import time as _time

        _time.sleep(1)
        result = start_trading()
        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send("🟡 IG Agent v25 restarted (via dashboard)")
        log_engine("agent/restart: trading loop restarted via API")
        return JSONResponse(
            {"ok": result.get("ok", False), "status": result.get("status")}
        )
    except Exception as e:
        log_engine(f"agent/restart failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── Heartbeat ─────────────────────────────────────────────────────────────────


def _start_heartbeat_monitor() -> None:
    """No-op: auto-shutdown on browser disconnect is disabled.

    The agent must run headless overnight; use POST /api/shutdown to stop it
    deliberately. This function is retained so existing call-sites compile.
    """


def _trigger_shutdown(source: str = "api") -> None:
    """Write a clean shutdown log entry then kill the process after a short delay."""
    from system.engine_log import log_engine

    log_engine(f"agent shutdown requested (source={source}) — exiting in 2s")
    try:
        from system.telegram_notifier import send_critical_alert

        send_critical_alert(f"🛑 Agent stopped (source: {source})")
    except Exception as e:
        log_engine(f"telegram shutdown alert failed: {type(e).__name__}: {e}")

    def _exit() -> None:
        delay = 0.5 if source == "dashboard" else 2.0
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_exit, name="shutdown-trigger", daemon=True).start()


@router.post("/api/v1/alpha/compile")
async def api_v1_alpha_compile(request: Request) -> JSONResponse:
    """
    In-process alpha matrix compile — allocates ``ig_agent_v30_alpha_matrix`` SHM
    inside the live agent PID (no external compile lock contention).
    """
    body: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:
        pass

    stride = int(body.get("stride") or 48)
    force = bool(body.get("force", True))

    from intelligence.matrix_prebaker import schedule_inprocess_alpha_compile

    result = schedule_inprocess_alpha_compile(stride=stride, force=force)
    status = 200 if result.get("ok") else 409
    return JSONResponse(result, status_code=status)


@router.post("/api/clear_inflight/{epic}")
def api_clear_inflight(epic: str) -> JSONResponse:
    """Clear any stale in-flight / pending-confirmation state for one epic.

    Use when an epic is stuck with 'Order confirmation unresolved' and no
    matching IG position exists.  Safe to call while the agent is running —
    the next gate-pass for the epic will attempt a fresh order.
    """
    from system.engine_log import log_engine

    try:
        from execution.entry_inflight import clear_entry
        from execution.pending_order_reconcile import get_pending, resolve_pending

        had_entry = False
        had_pending = False

        pending = get_pending(epic)
        if pending is not None:
            had_pending = True
            resolve_pending(epic, reason="manually cleared via API")

        # Also clear the entry-inflight tracker in case it's set
        clear_entry(epic)
        had_entry = True  # clear_entry is idempotent

        log_engine(
            f"clear_inflight API: {epic} — "
            f"pending={'yes' if had_pending else 'no'} cleared"
        )
        return JSONResponse(
            {
                "ok": True,
                "epic": epic,
                "pending_cleared": had_pending,
                "entry_inflight_cleared": had_entry,
            }
        )
    except Exception as e:
        log_engine(f"clear_inflight API failed for {epic}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/heartbeat")
def api_heartbeat() -> JSONResponse:
    """Browser keep-alive ping (every 30 s). Records last-seen time for dashboard liveness only."""
    global _last_heartbeat
    _last_heartbeat = time.time()
    return JSONResponse({"ok": True, "ts": _last_heartbeat})


def _finish_dashboard_shutdown() -> None:
    """Run full teardown then exit — must not block the HTTP response."""
    from system.engine_log import log_engine
    from system.shutdown_cleanup import perform_shutdown_cleanup

    try:
        perform_shutdown_cleanup(source="dashboard", skip_port_cleanup=True)
        log_engine("shutdown: initiated via dashboard Stop button")
        log_engine("shutdown: process exit")
    except Exception as e:
        log_engine(f"shutdown deferred cleanup failed: {type(e).__name__}: {e}")
    os._exit(0)


@router.get("/api/shutdown/verify-status")
def api_shutdown_verify_status() -> dict[str, Any]:
    """Fallback verify poll while :8081 is unavailable (reads last verify snapshot)."""
    from system.paths import data_dir

    path = data_dir() / "state" / "last_shutdown_verify.json"
    if not path.is_file():
        return {"ok": False, "status": "pending", "checks": [], "issues": []}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"ok": False, "status": "invalid"}
    except Exception:
        return {"ok": False, "status": "error", "checks": [], "issues": []}


@router.post("/api/shutdown")
def api_shutdown(background_tasks: BackgroundTasks) -> JSONResponse:
    """Graceful agent shutdown — stop trading, clean port/lock, exit process."""
    from system.engine_log import log_engine
    from system.shutdown_cleanup import mark_manual_stop, spawn_post_shutdown_verifier

    try:
        log_engine(
            "shutdown API invoked — dashboard Stop Agent confirmed "
            "(safe-to-leave does not call this endpoint)"
        )
        mark_manual_stop(source="dashboard")
        spawn_post_shutdown_verifier(os.getpid())
        background_tasks.add_task(_finish_dashboard_shutdown)
        try:
            from system.overnight_supervision import (
                launchd_watchdog_active,
                overnight_supervision_summary,
            )
            from system.supervision_monitor import evaluate_supervision_drift

            launchd_preserved = launchd_watchdog_active()
            drift = evaluate_supervision_drift()
            summary = overnight_supervision_summary()
            supervision_payload = {
                "supervision_drift_ok": bool(drift.get("ok")),
                "supervision_drift": drift,
                "supervision_warnings": drift.get("warnings") or [],
                "overnight_supervision": summary,
                "overnight_armed": bool(summary.get("overnight_armed")),
            }
        except Exception:
            launchd_preserved = False
            supervision_payload = {}
        return JSONResponse(
            {
                "ok": True,
                "status": "shutting_down",
                "supervision": supervision_payload,
                "cleanup_checks": [
                    {
                        "label": "Manual stop flagged",
                        "ok": True,
                        "detail": "watchdog will not auto-restart agent for 10 min",
                    },
                    {
                        "label": "Launchd supervision",
                        "ok": launchd_preserved,
                        "detail": (
                            "preserved — Safe to Leave survives Stop Agent"
                            if launchd_preserved
                            else "not loaded — run ./scripts/install_launchd.sh"
                        ),
                    },
                    {
                        "label": "Shutdown started",
                        "ok": True,
                        "detail": "cleanup running in background",
                    },
                ],
                # Always IPv4 loopback — verify server may not be reachable via localhost→::1.
                "verify_poll_url": "http://127.0.0.1:8081/shutdown-verify",
            }
        )
    except Exception as e:
        log_engine(f"shutdown failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/apex/system-monitor")
def get_system_monitor() -> dict[str, Any]:
    """Native operational telemetry console — 30-minute rolling log + funnel counters."""
    try:
        from apex.system_monitor import build_monitor_snapshot

        return build_monitor_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/apex/export-warmup-report")
def post_export_warmup_report() -> dict[str, Any]:
    """Export triage WAL performance track record to logs/warmup_report_latest.md."""
    try:
        from apex.system_monitor import export_warmup_report

        return export_warmup_report()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
