"""
HTTP routes — dashboard API (Section 4.5 Steps 8 + 13).
"""

from __future__ import annotations

import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

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
    get_signal_log,
    get_system_info,
    read_version_state,
    run_e2e_execution_check,
    run_safe_to_leave,
    run_system_tests,
)
from api.intelligence_data import (
    learning_status,
    replay_summary,
    run_replay_pipeline,
    shadow_today,
)
from api.snapshot_store import get_tick, snapshot_age_s_fast

# ── Heartbeat ────────────────────────────────────────────────────────────────
# Browser pings /api/heartbeat every 30 s. The endpoint is kept so the
# dashboard can use it as a liveness indicator, but missed pings no longer
# trigger a shutdown. Use POST /api/shutdown for deliberate agent termination.
HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TIMEOUT_SEC = 600  # retained for reference; not used for shutdown
_last_heartbeat: float = time.time()
_heartbeat_lock = threading.Lock()

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    age = snapshot_age_s_fast()
    return {
        "ok": True,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "api": "up",
        "snapshot_age_s": age,
    }


@router.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Operational health — served from a background-refreshed cache (non-blocking)."""
    status = get_cached_health_status()
    return {
        **status,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "snapshot_age_s": snapshot_age_s_fast(),
    }


@router.get("/api/startup/status")
def get_startup_status() -> dict[str, Any]:
    """Real-time startup phase progress — polled by the StartupSplash component."""
    from system.startup_tracker import get_status

    return get_status()


@router.get("/state")
def state() -> dict[str, Any]:
    """Full dashboard snapshot — same schema as WebSocket tick messages."""
    tick = get_tick()
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


@router.post("/api/emergency_stop")
def api_emergency_stop() -> dict[str, Any]:
    result = run_emergency_stop()
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


@router.get("/api/trades")
def api_trades(limit: int = 10) -> dict[str, Any]:
    trades = get_closed_trades(limit=min(100, max(1, limit)))
    points_total = sum(float(t.get("points_score") or 0) for t in trades)
    return {"trades": trades, "points_total": points_total}


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


@router.get("/api/system")
def api_system() -> dict[str, Any]:
    info = get_system_info()
    try:
        from trading.strictness_resolver import strictness_payload

        info["trading_strictness"] = strictness_payload()
    except Exception:
        pass
    return info


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
        return JSONResponse({"ok": True, "deal_id": deal_id, "result": result})
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

        cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
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
            close_dir = "SELL" if side == "BUY" else "BUY"
            try:
                rest.close_position(
                    deal_id,
                    direction=close_dir,
                    size=size,
                    epic=epic or None,
                    currency_code=cfg.currency_code,
                )
                closed.append(deal_id)
                log_engine(f"flatten_all: closed {epic} deal={deal_id}")
            except Exception as e:
                errors.append(f"{deal_id}: {e}")
                log_engine(f"flatten_all error {deal_id}: {e}")

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

        cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
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
            close_dir = "SELL" if side == "BUY" else "BUY"
            rest.close_position(
                deal_id,
                direction=close_dir,
                size=size,
                epic=epic,
                currency_code=cfg.currency_code,
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
