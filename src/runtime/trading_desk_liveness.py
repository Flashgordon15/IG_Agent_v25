"""
Trading Desk AI liveness — connection failsafes and auto-recovery for live trades.

Detects stale IG sync, inactive position supervision, and unmonitored opens;
nudges background recovery without blocking API threads on REST.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_last_snapshot: dict[str, Any] = {}
_recovery_count = 0
_degraded_streak = 0
_last_recovery_mono = 0.0

RECOVERY_INTERVAL_SEC = 15.0
STALE_SYNC_SEC = 30.0
STALE_MANAGER_TICK_SEC = 45.0
RECOVERY_COOLDOWN_SEC = 8.0


def _sync_age_sec(sync: Any) -> float | None:
    try:
        ts = float(getattr(sync, "_last_sync_ts", 0) or 0)
        if ts > 0:
            return max(0.0, time.time() - ts)
    except Exception:
        pass
    return None


def evaluate_liveness() -> dict[str, Any]:
    """Read-only liveness snapshot — never blocks on REST."""
    global _degraded_streak
    issues: list[str] = []
    connections: dict[str, bool] = {"agent_api": True}
    sync_age: float | None = None
    sync_status = ""
    mgr_active = False
    mgr_tick_age: float | None = None
    unmonitored = 0
    open_count = 0
    gbp_tracks = 0

    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
    except Exception:
        sync = None

    if sync is None:
        issues.append("ig_sync_missing")
        connections["ig_sync"] = False
    else:
        snap = sync.snapshot()
        sync_status = str(getattr(snap, "sync_status", "") or "")
        sync_age = _sync_age_sec(sync)
        fresh = bool(sync.is_fresh())
        connections["ig_sync"] = fresh
        open_count = int(getattr(snap, "total_open", 0) or 0)
        if not fresh:
            issues.append("ig_sync_stale")
        if str(getattr(snap, "last_error", "") or "").strip():
            issues.append("ig_sync_error")

    try:
        from runtime.open_position_manager import snapshot as mgr_snap

        mgr = mgr_snap()
        mgr_active = bool(mgr.get("active"))
        last_tick = float(mgr.get("last_tick_at") or 0)
        if last_tick > 0:
            mgr_tick_age = max(0.0, time.time() - last_tick)
        connections["position_manager"] = mgr_active and (
            mgr_tick_age is None or mgr_tick_age <= STALE_MANAGER_TICK_SEC
        )
        if not mgr_active:
            issues.append("position_manager_inactive")
        elif mgr_tick_age is not None and mgr_tick_age > STALE_MANAGER_TICK_SEC:
            issues.append("position_manager_stale")
        if str(mgr.get("last_error") or "").strip():
            issues.append("position_manager_error")
    except Exception:
        connections["position_manager"] = False
        issues.append("position_manager_unavailable")

    live_count = 0
    ts_broker_open = 0
    verdict = ""
    try:
        from api.positions_live import build_live_positions_payload

        live = build_live_positions_payload()
        live_count = int(live.get("count") or 0)
        unmonitored = int(live.get("unmonitored") or 0)
        if live_count > 0 and unmonitored > 0:
            issues.append(f"unmonitored_positions:{unmonitored}")
        if live.get("stale"):
            issues.append("positions_snapshot_stale")
        verdict = str(live.get("verdict") or "")
        connections["positions_live"] = verdict in ("HEALTHY", "FLAT")
        if verdict == "DEGRADED":
            issues.append("positions_degraded")
        if verdict == "CRITICAL" or live.get("critical"):
            issues.append("positions_critical")
            connections["positions_live"] = False
            for alarm in (live.get("critical_alarms") or [])[:4]:
                issues.append(str(alarm)[:120])
        ts_block = live.get("trade_support") or {}
        try:
            ts_broker_open = int(ts_block.get("broker_open") or 0)
        except (TypeError, ValueError):
            ts_broker_open = 0
        if ts_block.get("actions_failed"):
            issues.append(
                f"flatten_failed:{ts_block.get('last_flatten_error') or 'unknown'}"
            )
    except Exception as exc:
        connections["positions_live"] = False
        issues.append(f"positions_live_error:{type(exc).__name__}")

    try:
        from runtime.micro_gbp_exit import snapshot as gbp_snap

        gbp_tracks = len((gbp_snap().get("tracks") or {}))
    except Exception:
        pass

    # Align open_count with broker-authoritative surfaces (sync alone can be empty).
    open_count = max(open_count, live_count, ts_broker_open, gbp_tracks)
    has_open_risk = open_count > 0

    # Flat-book soft issues must NOT trip degraded forever or drive REST recovery
    # storms. trade_support + positions_live FLAT is broker SoT when sync is absent;
    # OPM tick_timeout / false snapshot-stale on an empty book is noise, not
    # open-risk failure.
    flat_sot = (
        not has_open_risk
        and live_count == 0
        and ts_broker_open == 0
        and verdict in ("FLAT", "HEALTHY", "")
    )
    if not has_open_risk:
        mgr_err = ""
        try:
            from runtime.open_position_manager import snapshot as _mgr_err_snap

            mgr_err = str((_mgr_err_snap() or {}).get("last_error") or "")
        except Exception:
            mgr_err = ""
        soft: list[str] = []
        kept: list[str] = []
        for issue in issues:
            if issue == "ig_sync_missing" and connections.get("positions_live"):
                soft.append(issue)
                connections["ig_sync"] = True
                continue
            if issue in ("position_manager_error", "position_manager_stale"):
                if "tick_timeout" in mgr_err or not mgr_err:
                    soft.append(issue)
                    connections["position_manager"] = True
                    continue
            # Cache/sync age noise on a broker-flat book must not red-line
            # liveness or drive desk_support recover_and_supervise storms.
            if issue == "positions_snapshot_stale" and flat_sot:
                soft.append(issue)
                continue
            if issue in ("ig_sync_stale", "positions_degraded") and flat_sot:
                soft.append(issue)
                if issue == "ig_sync_stale":
                    connections["ig_sync"] = True
                continue
            kept.append(issue)
        if soft:
            issues = kept

    ok = len(issues) == 0
    if ok:
        _degraded_streak = 0
    else:
        _degraded_streak += 1

    out = {
        "ok": ok,
        "connections": connections,
        "issues": issues,
        "degraded_streak": _degraded_streak,
        "recovery_count": _recovery_count,
        "has_open_risk": has_open_risk,
        "sync_age_sec": sync_age,
        "sync_status": sync_status,
        "manager_tick_age_sec": mgr_tick_age,
        "unmonitored": unmonitored,
        "open_count": open_count,
        "gbp_tracks": gbp_tracks,
        "ts": time.time(),
    }
    with _lock:
        _last_snapshot = dict(out)
    return out


def run_recovery_tick(*, force: bool = False) -> dict[str, Any]:
    """Attempt non-blocking recovery for detected issues."""
    global _recovery_count, _last_recovery_mono
    now = time.monotonic()
    if not force and now - _last_recovery_mono < RECOVERY_COOLDOWN_SEC:
        return {"skipped": True, "reason": "cooldown", "liveness": evaluate_liveness()}

    liv = evaluate_liveness()
    actions: list[str] = []
    has_open_risk = bool(liv.get("has_open_risk"))

    if "ig_sync_stale" in liv["issues"] or "ig_sync_error" in liv["issues"]:
        try:
            from runtime.agent_bootstrap import get_ig_position_sync

            sync = get_ig_position_sync()
            if sync is not None:
                sync.request_refresh()
                actions.append("ig_sync_refresh")
        except Exception as exc:
            actions.append(f"ig_sync_refresh_failed:{type(exc).__name__}")

    # Flat book: never hammer GET /positions via OPM tick / risk_stack reconcile.
    # That path exhausted ChaosGuardian tokens and starved micro order dispatch.
    if not has_open_risk:
        # Flat book: never hammer GET /positions via OPM tick / risk_stack reconcile.
        # That path exhausted ChaosGuardian tokens and starved micro order dispatch.
        # Still re-arm the OPM daemon if it died after deferred auth / recycle —
        # supervision must be live before the next open appears.
        try:
            from runtime.open_position_manager import (
                ensure_open_position_manager,
                snapshot as mgr_snap,
            )
            from system.config_loader import get_config
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import get_shared_rest_client

            mgr = mgr_snap() or {}
            if not mgr.get("active"):
                rest = None
                cred = try_load_credentials()
                if cred.ok and cred.credentials:
                    rest = get_shared_rest_client(cred.credentials)
                ensured = ensure_open_position_manager(rest, cfg=get_config())
                if ensured.get("ok"):
                    actions.append(
                        "position_manager_ensure"
                        + (":rearmed" if ensured.get("rearmed") else "")
                    )
                else:
                    actions.append("position_manager_ensure_failed")
        except Exception as exc:
            actions.append(f"position_manager_ensure_failed:{type(exc).__name__}")

        try:
            from runtime.active_lifecycle_trades import reconcile_active_lifecycle_trades
            from data.learning_store import LearningStore
            from system.config_loader import get_config

            cfg = get_config()
            db = str(getattr(cfg, "learning_db", "") or "")
            if db:
                store = LearningStore(db)
                counts = reconcile_active_lifecycle_trades(
                    store, [], source="liveness_flat"
                )
                closed_n = int(counts.get("closed_registry") or 0)
                if closed_n:
                    actions.append(f"lifecycle_flat_reconcile:closed={closed_n}")
        except Exception as exc:
            actions.append(f"lifecycle_flat_reconcile_failed:{type(exc).__name__}")

        if actions:
            _recovery_count += 1
            _last_recovery_mono = now
            log_engine(
                f"TradingDeskLiveness: flat recovery actions={actions} "
                f"issues={liv.get('issues')}"
            )
        return {"actions": actions, "liveness": evaluate_liveness()}

    unmonitored = int(liv.get("unmonitored") or 0)
    needs_mgr = any(
        i in liv["issues"]
        for i in (
            "position_manager_inactive",
            "position_manager_stale",
            "position_manager_error",
            "position_manager_unavailable",
            "positions_degraded",
            "positions_snapshot_stale",
        )
    ) or unmonitored > 0 or (
        has_open_risk and not liv.get("connections", {}).get("ig_sync")
    )

    if needs_mgr:
        try:
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
            ensured = ensure_open_position_manager(rest, cfg=cfg)
            if ensured.get("rearmed"):
                actions.append("position_manager_ensure:rearmed")
            if rest is not None:
                run_management_tick(rest, cfg, execute=True)
                actions.append("position_manager_tick")
        except Exception as exc:
            actions.append(f"position_manager_tick_failed:{type(exc).__name__}")

    if unmonitored > 0:
        try:
            from execution.position_risk_stack import reconcile_open_positions_risk_stack
            from system.config_loader import get_config
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import get_shared_rest_client

            cred = try_load_credentials()
            if cred.ok and cred.credentials:
                rest = get_shared_rest_client(cred.credentials)
                counts = reconcile_open_positions_risk_stack(
                    rest, cfg=get_config(), force=True
                )
                actions.append(f"risk_stack_reconcile:{counts.get('armed', 0)}")
        except Exception as exc:
            actions.append(f"risk_stack_failed:{type(exc).__name__}")

    # Permanent feed self-heal: bridge Yahoo → hub when entry path is starve-closed.
    try:
        from runtime.desk_self_assess import bridge_stale_hub_from_yahoo

        heal = bridge_stale_hub_from_yahoo(force=False)
        if heal.get("bridged"):
            actions.append(f"hub_yahoo_bridge:{len(heal.get('bridged') or [])}")
    except Exception as exc:
        actions.append(f"desk_self_assess_failed:{type(exc).__name__}")

    if actions:
        _recovery_count += 1
        _last_recovery_mono = now
        log_engine(
            f"TradingDeskLiveness: recovery actions={actions} "
            f"issues={liv.get('issues')}"
        )

    return {"actions": actions, "liveness": evaluate_liveness()}


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_last_snapshot) if _last_snapshot else evaluate_liveness()


def start_trading_desk_liveness_monitor() -> None:
    """Daemon: evaluate and recover Trading Desk connections during live risk."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()

    def _loop() -> None:
        while not _stop.wait(RECOVERY_INTERVAL_SEC):
            try:
                liv = evaluate_liveness()
                if not liv.get("ok"):
                    run_recovery_tick()
                elif liv.get("has_open_risk"):
                    age = liv.get("sync_age_sec")
                    if age is not None and float(age) > STALE_SYNC_SEC:
                        run_recovery_tick()
            except Exception as exc:
                log_engine(
                    f"TradingDeskLiveness: monitor error {type(exc).__name__}: {exc}"
                )

    _thread = threading.Thread(
        target=_loop, name="trading-desk-liveness", daemon=True
    )
    _thread.start()
    log_engine("TradingDeskLiveness: monitor armed")


def stop_trading_desk_liveness_monitor() -> None:
    _stop.set()


def reset_trading_desk_liveness_for_tests() -> None:
    global _last_snapshot, _recovery_count, _degraded_streak, _last_recovery_mono
    stop_trading_desk_liveness_monitor()
    with _lock:
        _last_snapshot = {}
        _recovery_count = 0
        _degraded_streak = 0
        _last_recovery_mono = 0.0
