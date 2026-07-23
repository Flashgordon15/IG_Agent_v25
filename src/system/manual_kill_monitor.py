"""Master manual kill switch — flatten positions and hard-block all gates."""

from __future__ import annotations

import threading
import time

from system.engine_log import log_engine
from system.qmm_process_supervisor import set_process_entry_block

_MONITOR_INTERVAL_SEC = 2.0
_MASTER_KILL_REASON = "MASTER_KILL_SWITCH_ACTIVE"
_last_seen_active = False
_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_flatten_in_flight = False


def is_master_kill_block_active() -> bool:
    from system.shutdown_cleanup import manual_stop_active

    return manual_stop_active()


def _flatten_open_positions_nonblocking() -> None:
    global _flatten_in_flight
    with _lock:
        if _flatten_in_flight:
            return
        _flatten_in_flight = True

    def _worker() -> None:
        global _flatten_in_flight
        try:
            from system.config_loader import ConfigLoader
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import ensure_shared_authenticated
            from system.paths import config_dir

            status = try_load_credentials()
            if not status.ok or status.credentials is None:
                log_engine("manual_kill: credentials missing — skip flatten")
                return
            rest = ensure_shared_authenticated(status.credentials)
            positions = rest.open_positions() if hasattr(rest, "open_positions") else []
            closed = 0
            from system.config_loader import load_active_config

            cfg = load_active_config(validate=False)
            for item in positions or []:
                pos = item.get("position") or {}
                mkt = item.get("market") or {}
                deal_id = str(pos.get("dealId") or "")
                epic = str(mkt.get("epic") or "")
                side = str(pos.get("direction") or "BUY").upper()
                size = float(pos.get("size") or 0)
                if not deal_id or size <= 0:
                    continue
                # close_position expects OPEN side and inverts once — never pass close_dir.
                rest.close_position(
                    deal_id,
                    direction=side,
                    size=size,
                    epic=epic or None,
                    currency_code=cfg.currency_code,
                )
                closed += 1
            log_engine(f"manual_kill: non-blocking flatten closed={closed}")
        except Exception as e:
            log_engine(f"manual_kill: flatten failed: {type(e).__name__}: {e}")
        finally:
            with _lock:
                _flatten_in_flight = False

    threading.Thread(
        target=_worker, name="manual-kill-flatten", daemon=True
    ).start()


def _apply_loop_entry_blocks(active: bool) -> None:
    try:
        from runtime.market_orchestrator import MarketOrchestrator

        ref = getattr(MarketOrchestrator, "_ORCHESTRATOR_REF", None)
        if ref is None:
            from runtime import market_orchestrator as mo

            ref = mo._ORCHESTRATOR_REF
        if ref is None:
            return
        for loop in getattr(ref, "_loops", []) or []:
            if active:
                loop.set_entry_circuit_breaker(_MASTER_KILL_REASON)
            else:
                loop.clear_entry_circuit_breaker()
    except Exception:
        pass


def _monitor_tick() -> None:
    global _last_seen_active
    try:
        from system.agent_execution_mode import demo_sandbox_unblock_active

        if demo_sandbox_unblock_active():
            if _last_seen_active:
                from system.qmm_process_supervisor import clear_process_entry_block

                clear_process_entry_block()
                _apply_loop_entry_blocks(False)
                _last_seen_active = False
            return
    except Exception:
        pass
    active = is_master_kill_block_active()
    if active:
        set_process_entry_block(_MASTER_KILL_REASON)
        _apply_loop_entry_blocks(True)
        if not _last_seen_active:
            log_engine("manual_kill: MASTER KILL engaged — flattening + gate hard-block")
            _flatten_open_positions_nonblocking()
    else:
        if _last_seen_active:
            log_engine("manual_kill: manual stop cleared — releasing process entry block")
            from system.qmm_process_supervisor import clear_process_entry_block

            clear_process_entry_block()
            _apply_loop_entry_blocks(False)
    _last_seen_active = active


def _monitor_loop() -> None:
    while not _stop.wait(_MONITOR_INTERVAL_SEC):
        try:
            _monitor_tick()
        except Exception as e:
            log_engine(f"manual_kill monitor: {type(e).__name__}: {e}")


def start_manual_kill_monitor() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_monitor_loop, name="manual-kill-monitor", daemon=True
    )
    _thread.start()
    log_engine("manual_kill: monitor started")


def stop_manual_kill_monitor() -> None:
    _stop.set()


def reset_manual_kill_monitor_for_tests() -> None:
    global _last_seen_active, _flatten_in_flight
    stop_manual_kill_monitor()
    _last_seen_active = False
    _flatten_in_flight = False
    from system.qmm_process_supervisor import reset_qmm_process_supervisor_for_tests

    reset_qmm_process_supervisor_for_tests()
