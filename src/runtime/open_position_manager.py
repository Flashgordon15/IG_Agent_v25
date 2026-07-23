"""
Dedicated open-position supervisor — permanent in-process management.

Consolidates IG sync refresh, risk-stack coverage, GBP exit pumping, and
rule-based flatten actions so winners bank and losers cut without operator
intervention or CLI scripts.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from execution.open_position_actions import execute_actions_bulk
from execution.open_position_rules import (
    assess_open_positions,
    position_management_cfg,
    rows_from_ig_items,
    rows_from_snapshot_positions,
    rows_from_sync_positions,
)
from system.engine_log import log_engine

_lock = threading.RLock()
_state_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_rest: Any | None = None
_cfg: Any | None = None
_active = False
_last_tick_at = 0.0
_tick_count = 0
_last_report: dict[str, Any] = {}
_last_error = ""
_tick_running = False
_tick_started_at = 0.0
_DEFAULT_TICK_TIMEOUT_SEC = 45.0
_DEFAULT_TICK_STALE_SEC = 90.0


def start_open_position_manager(
    rest_client: Any | None,
    *,
    cfg: Any | None = None,
) -> None:
    global _rest, _cfg, _thread, _active
    pm = position_management_cfg(cfg)
    if not pm.get("manager_enabled", True):
        log_engine("OpenPositionManager: disabled in config")
        return

    with _lock:
        _rest = rest_client
        _cfg = cfg
        if _thread is not None and _thread.is_alive():
            _active = True
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_run_loop,
            name="open-position-manager",
            daemon=True,
        )
        _active = True
        _thread.start()
        threading.Thread(
            target=_first_tick,
            name="open-position-manager-boot",
            daemon=True,
        ).start()
    log_engine("OpenPositionManager: supervisor armed")


def _first_tick() -> None:
    """Run one management cycle shortly after boot (hydrate + bank winners).

    Always force risk-stack coverage for broker opens so mid-session deploys
    adopt inflight trades automatically (entries may still be held separately).
    """
    time.sleep(3.0)
    if _stop.is_set():
        return
    try:
        if _rest is not None and _cfg is not None:
            try:
                from data.learning_store import LearningStore
                from runtime.active_lifecycle_trades import boot_reconcile_active_trades
                from system.paths import data_dir

                store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
                boot_reconcile_active_trades(_rest, store)
            except Exception:
                pass
            try:
                from execution.position_risk_stack import (
                    ensure_risk_stack_coverage,
                    reconcile_open_positions_risk_stack,
                )

                ensure_risk_stack_coverage(_rest, cfg=_cfg, force=True)
                reconcile_open_positions_risk_stack(_rest, cfg=_cfg, force=True)
            except Exception as arm_exc:
                log_engine(
                    f"OpenPositionManager: boot arm skipped: "
                    f"{type(arm_exc).__name__}: {arm_exc}"
                )
        run_management_tick(_rest, _cfg, execute=True)
    except Exception as exc:
        log_engine(f"OpenPositionManager: boot tick failed: {type(exc).__name__}: {exc}")


def stop_open_position_manager() -> None:
    global _active
    _stop.set()
    _active = False


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "active": _active,
            "last_tick_at": _last_tick_at,
            "tick_count": _tick_count,
            "last_error": _last_error,
            "last_report": dict(_last_report),
        }


def reset_open_position_manager_for_tests() -> None:
    global _rest, _cfg, _active, _last_tick_at, _tick_count, _last_report, _last_error
    global _tick_running, _tick_started_at
    stop_open_position_manager()
    with _lock:
        _rest = None
        _cfg = None
        _last_tick_at = 0.0
        _tick_count = 0
        _last_report = {}
        _last_error = ""
    with _state_lock:
        _tick_running = False
        _tick_started_at = 0.0


def _tick_timeout_sec(cfg: Any | None) -> float:
    pm = position_management_cfg(cfg or _cfg)
    desk = {}
    try:
        raw = cfg or _cfg
        if raw is not None:
            desk = (
                raw.get("desk_deploy")
                if isinstance(raw, dict)
                else getattr(raw, "desk_deploy", None) or {}
            )
            if not isinstance(desk, dict):
                desk = {}
    except Exception:
        desk = {}
    return max(
        5.0,
        float(
            pm.get("manager_tick_timeout_sec")
            or desk.get("tick_timeout_sec")
            or _DEFAULT_TICK_TIMEOUT_SEC
        ),
    )


def _tick_stale_sec(cfg: Any | None) -> float:
    pm = position_management_cfg(cfg or _cfg)
    desk = {}
    try:
        raw = cfg or _cfg
        if raw is not None:
            desk = (
                raw.get("desk_deploy")
                if isinstance(raw, dict)
                else getattr(raw, "desk_deploy", None) or {}
            )
            if not isinstance(desk, dict):
                desk = {}
    except Exception:
        desk = {}
    return max(
        1.0,
        float(
            pm.get("manager_tick_stale_sec")
            or desk.get("tick_stale_sec")
            or _DEFAULT_TICK_STALE_SEC
        ),
    )


def _record_tick_result(result: dict[str, Any]) -> None:
    global _last_tick_at, _tick_count, _last_report, _last_error
    with _lock:
        _last_tick_at = time.time()
        _tick_count += 1
        _last_report = dict(result)
        if result.get("ok"):
            _last_error = ""
        else:
            _last_error = str(result.get("error") or "")


def run_management_tick(
    rest_client: Any | None = None,
    cfg: Any | None = None,
    *,
    execute: bool = True,
) -> dict[str, Any]:
    """Single assess + optional execute cycle (used by daemon and tests)."""
    global _tick_running, _tick_started_at

    cfg = cfg or _cfg
    timeout_sec = _tick_timeout_sec(cfg)
    stale_sec = _tick_stale_sec(cfg)

    with _state_lock:
        if _tick_running:
            age = time.time() - _tick_started_at if _tick_started_at > 0 else 0.0
            # Abandon after timeout(+grace), not only stale_sec — a hung REST
            # tick must not block supervision for a full 90s while opens are live.
            abandon_after = min(stale_sec, max(10.0, timeout_sec + 5.0))
            if age < abandon_after:
                return {
                    "ok": False,
                    "error": "tick_in_progress",
                    "skipped": True,
                    "in_flight_sec": round(age, 1),
                }
            log_engine(
                f"OpenPositionManager: abandoning stale in-flight tick "
                f"({age:.0f}s > {abandon_after:.0f}s)"
            )
            _tick_running = False

        _tick_running = True
        _tick_started_at = time.time()

    result_holder: dict[str, Any] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result_holder["result"] = _run_management_tick_impl(
                rest_client=rest_client, cfg=cfg, execute=execute
            )
        except Exception as exc:
            result_holder["result"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            done.set()

    worker = threading.Thread(target=_worker, name="opm-tick", daemon=True)
    worker.start()
    completed = done.wait(timeout=timeout_sec)

    if completed:
        result = result_holder.get("result") or {"ok": False, "error": "empty_tick"}
    else:
        # Flat book: a hung tick must not sticky-error the desk into REST recovery.
        flat_book = False
        try:
            from pathlib import Path
            import json

            from system.paths import data_dir

            snap_path = Path(data_dir()) / "state" / "broker_snapshot.json"
            if snap_path.is_file():
                raw = json.loads(snap_path.read_text(encoding="utf-8") or "{}")
                flat_book = int(raw.get("count") or 0) == 0
        except Exception:
            flat_book = False
        if flat_book:
            result = {
                "ok": True,
                "error": "",
                "timed_out": True,
                "timeout_sec": timeout_sec,
                "flat_book_soft_ok": True,
                "note": "tick_timeout_ignored_flat_book",
            }
            log_engine(
                f"OpenPositionManager: tick timed out after {timeout_sec:.0f}s "
                "but broker book is flat — soft-ok (no stop fallback REST)"
            )
        else:
            result = {
                "ok": False,
                "error": "tick_timeout",
                "timed_out": True,
                "timeout_sec": timeout_sec,
            }
            log_engine(
                f"OpenPositionManager: tick timed out after {timeout_sec:.0f}s — "
                "releasing supervision slot; attaching broker stops best-effort"
            )
            # Software stack may be stuck — ensure IG-side stop exists when possible.
            try:
                stop_report = _attach_broker_stops_on_timeout(rest_client, cfg)
                result["broker_stop_fallback"] = stop_report
            except Exception as exc:
                result["broker_stop_fallback"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            try:
                from execution.edits_only_close_queue import drain_when_tradeable

                if rest_client is not None:
                    result["edits_only_drain"] = drain_when_tradeable(rest_client, cfg)
            except Exception:
                pass

    with _state_lock:
        _tick_running = False
        _tick_started_at = 0.0

    _record_tick_result(result if isinstance(result, dict) else {"ok": False})
    return result


def _attach_broker_stops_on_timeout(
    rest_client: Any | None,
    cfg: Any | None,
) -> dict[str, Any]:
    """Best-effort broker stop attach when OPM tick hangs (EDITS_ONLY-safe PUT)."""
    if rest_client is None:
        return {"ok": False, "error": "no_rest", "armed": 0}
    armed = 0
    errors: list[str] = []
    try:
        from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic
    except Exception:
        resolve_micro_tp_sl_for_epic = None  # type: ignore

    try:
        items = list(rest_client.open_positions(budget_priority=False) or [])
    except Exception as exc:
        return {"ok": False, "error": f"list:{type(exc).__name__}", "armed": 0}

    for item in items[:12]:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        epic = str(mkt.get("epic") or "").strip()
        if not deal_id or not epic:
            continue
        entry = float(pos.get("level") or pos.get("openLevel") or 0)
        if entry <= 0:
            errors.append(f"{deal_id[:10]}:entry<=0")
            continue
        size = float(pos.get("size") or 0.5)
        stop_pts = 4.0
        limit_pts = 0.0
        if resolve_micro_tp_sl_for_epic is not None:
            try:
                tp, sl, _ = resolve_micro_tp_sl_for_epic(epic, size, cfg)
                stop_pts = float(sl or stop_pts)
                limit_pts = float(tp or 0)
            except Exception:
                pass
        try:
            ok = bool(
                rest_client.ensure_protective_stops(
                    deal_id,
                    epic=epic,
                    stop_distance=stop_pts,
                    limit_distance=limit_pts,
                )
            )
            if ok:
                armed += 1
        except Exception as exc:
            errors.append(f"{deal_id[:10]}:{type(exc).__name__}")
    return {"ok": True, "armed": armed, "errors": errors[:8]}


def _run_management_tick_impl(
    rest_client: Any | None = None,
    cfg: Any | None = None,
    *,
    execute: bool = True,
) -> dict[str, Any]:
    rest = rest_client or _rest
    cfg = cfg or _cfg
    if rest is None:
        return {"ok": False, "error": "no_rest_client"}

    pm = position_management_cfg(cfg)

    _ensure_sub_engines(rest, cfg)

    # B1: air-gapped broker UPL hard floor before local entry/mark assessment.
    hard_floor_report: dict[str, Any] = {}
    if execute:
        try:
            from execution.broker_upl_hard_floor import scan_and_request_hard_floor_flattens

            hard_floor_report = scan_and_request_hard_floor_flattens(rest, cfg)
            if hard_floor_report.get("triggered"):
                log_engine(
                    f"OpenPositionManager: hard_floor triggered="
                    f"{hard_floor_report.get('triggered')}"
                )
        except Exception as exc:
            log_engine(
                f"OpenPositionManager: hard_floor scan failed "
                f"{type(exc).__name__}: {exc} — fail-safe continue"
            )
            hard_floor_report = {"error": f"{type(exc).__name__}: {exc}"}

    rows, source, sync_age = _fetch_open_rows(rest, cfg)
    report = assess_open_positions(
        rows,
        cfg,
        gbp_tracks=_gbp_tracks(),
        agent_up=True,
        source=source,
        sync_age_sec=sync_age,
    )

    executed = 0
    if execute and report.actions:
        execute_actions_bulk(rest, report, cfg)
        executed = sum(1 for a in report.actions if a.ok)

    unmonitored = 0
    try:
        gbp = _gbp_tracks()
        broker_ids = {getattr(r, "deal_id", "") for r in rows}
        unmonitored = sum(1 for did in broker_ids if did and did not in gbp)
    except Exception:
        unmonitored = 0

    if unmonitored > 0:
        try:
            from execution.position_risk_stack import (
                ensure_risk_stack_coverage,
                reconcile_open_positions_risk_stack,
            )

            ensure_risk_stack_coverage(rest, cfg=cfg, force=True)
            counts = reconcile_open_positions_risk_stack(rest, cfg=cfg, force=True)
            report.issues.append(
                f"unmonitored_escalation armed={counts.get('armed', 0)} "
                f"gbp={counts.get('gbp', 0)}"
            )
            log_engine(
                f"OpenPositionManager: unmonitored={unmonitored} — risk stack forced"
            )
        except Exception as exc:
            report.issues.append(f"unmonitored_escalation_failed:{type(exc).__name__}")

    if report.actions:
        log_engine(
            f"OpenPositionManager: {len(report.actions)} action(s) "
            f"source={source} executed={executed} "
            f"broker_open={report.broker_open}"
        )
        for act in report.actions[:5]:
            status = "ok" if act.ok else act.error or "pending"
            log_engine(
                f"  {act.deal_id[:12]} {act.action} {act.reason} [{status}]"
            )

    try:
        from runtime.micro_gbp_exit import on_watchdog_tick

        on_watchdog_tick()
    except Exception:
        pass

    out = {
        "ok": True,
        "source": source,
        "sync_age_sec": sync_age,
        "broker_open": report.broker_open,
        "assessed": report.assessed,
        "actions_queued": len(report.actions),
        "actions_executed": executed,
        "hard_floor": hard_floor_report,
        "unmonitored": unmonitored,
        "issues": report.issues[:10],
        "positions": report.positions[:20],
        "actions": [
            {
                "deal_id": a.deal_id,
                "epic": a.epic,
                "pnl_gbp": a.pnl_gbp,
                "action": a.action,
                "reason": a.reason,
                "ok": a.ok,
                "error": a.error,
            }
            for a in report.actions
        ],
    }
    return out


def _poll_interval_sec(pm: dict[str, Any]) -> float:
    return max(5.0, float(pm.get("manager_poll_sec") or 10.0))


def _run_loop() -> None:
    while not _stop.is_set():
        pm = position_management_cfg(_cfg)
        poll_sec = _poll_interval_sec(pm)
        try:
            run_management_tick(_rest, _cfg, execute=True)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log_engine(f"OpenPositionManager: tick failed: {err}")
            _record_tick_result({"ok": False, "error": err})
        _stop.wait(poll_sec)


def _ensure_sub_engines(rest: Any, cfg: Any) -> None:
    from execution.position_risk_stack import (
        ensure_risk_stack_coverage,
        reconcile_open_positions_risk_stack,
    )
    from runtime.dynamic_limit_engine import start_dynamic_limit_engine
    from runtime.micro_gbp_exit import start_micro_gbp_exit_engine
    from runtime.virtual_stop_loss import start_virtual_stop_watchdog

    pm = position_management_cfg(cfg)
    start_micro_gbp_exit_engine(rest)
    start_virtual_stop_watchdog(rest)
    start_dynamic_limit_engine()
    force_stack = bool(pm.get("require_full_risk_stack", False))
    if force_stack:
        reconcile_open_positions_risk_stack(rest, cfg=cfg, force=True)
    else:
        ensure_risk_stack_coverage(rest, cfg=cfg, force=False)


def _gbp_tracks() -> dict[str, Any]:
    try:
        from runtime.micro_gbp_exit import snapshot as gbp_snap

        return gbp_snap().get("tracks") or {}
    except Exception:
        return {}


def _fetch_open_rows(
    rest: Any,
    cfg: Any,
) -> tuple[list[Any], str, float | None]:
    """Return assessment rows, data source label, sync age seconds."""
    pm = position_management_cfg(cfg)
    gbp_tracks = _gbp_tracks()
    sync_age: float | None = None

    # Shared broker snapshot — never blocks on REST budget (hot path for manager).
    try:
        from runtime import broker_snapshot

        shared = broker_snapshot.read_snapshot(max_age_sec=15.0)
        if shared and shared.get("positions"):
            rows = rows_from_snapshot_positions(
                list(shared.get("positions") or []),
                cfg,
                gbp_tracks=gbp_tracks,
            )
            if rows:
                return rows, f"broker_snapshot({shared.get('source')})", sync_age
    except Exception:
        pass

    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
    except Exception:
        sync = None

    if sync is not None:
        snap = sync.snapshot()
        fresh = bool(sync.is_fresh())
        sync_age = None
        try:
            last_ts = float(getattr(sync, "_last_sync_ts", 0) or 0)
            if last_ts > 0:
                sync_age = max(0.0, time.time() - last_ts)
        except Exception:
            pass

        positions = list(getattr(snap, "positions", []) or [])

        if not positions or not fresh:
            if pm.get("force_sync_on_stale", True):
                try:
                    sync.request_refresh()
                except Exception:
                    pass

        if positions and fresh:
            rows = rows_from_sync_positions(
                positions, cfg, gbp_tracks=gbp_tracks, sync=sync
            )
            return rows, "sync_cache", sync_age

        broker_tracks = len(gbp_tracks)
        if broker_tracks > 0:
            log_engine(
                f"OpenPositionManager: sync empty but {broker_tracks} GBP track(s) — REST fallback"
            )

    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        rest_timeout = max(3.0, float(pm.get("rest_fallback_timeout_sec") or 8.0))

        def _rest_fetch() -> list[Any]:
            return list(rest.open_positions(budget_priority=True) or [])

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_rest_fetch)
            try:
                items = future.result(timeout=rest_timeout)
            except FuturesTimeout:
                log_engine(
                    f"OpenPositionManager: REST positions timed out after {rest_timeout:.0f}s"
                )
                return [], "rest_timeout", sync_age
        rows = rows_from_ig_items(items, cfg, gbp_tracks=gbp_tracks)
        if rows:
            try:
                from runtime import broker_snapshot

                broker_snapshot.write_snapshot(source="open_position_manager", items=items)
            except Exception:
                pass
        return rows, "rest", sync_age
    except Exception as exc:
        log_engine(
            f"OpenPositionManager: REST positions failed: {type(exc).__name__}: {exc}"
        )
        return [], "none", sync_age
