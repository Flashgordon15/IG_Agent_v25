"""Emergency cockpit override — flatten + halt execution routing."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from system.engine_log import log_engine


_flatten_lock = threading.Lock()
_inflight = False


def execute_emergency_cockpit_override() -> dict[str, str]:
    """
    Stop trading loops and market-close all IG positions via REST.

    Non-blocking for the cockpit UI — runs heavy work on a daemon thread
    when invoked from the command queue worker (already background).
    """
    global _inflight
    with _flatten_lock:
        if _inflight:
            return {"status": "already_running"}
        _inflight = True

    status: dict[str, str] = {"status": "ok"}
    try:
        log_engine("COCKPIT EMERGENCY OVERRIDE — halting execution + flatten all")

        try:
            from api.agent_control import stop_trading

            stop_trading()
        except Exception as e:
            status["stop_trading"] = f"{type(e).__name__}: {e}"

        try:
            from system.qmm_process_supervisor import set_process_entry_block

            set_process_entry_block("COCKPIT_EMERGENCY_OVERRIDE")
        except Exception as e:
            status["entry_block"] = f"{type(e).__name__}: {e}"

        try:
            from system.shutdown_cleanup import mark_manual_stop

            mark_manual_stop(source="cockpit_emergency")
        except Exception as e:
            status["manual_stop"] = f"{type(e).__name__}: {e}"

        closed = 0
        try:
            from ig_api.rest_client import IGRestClient
            from system.credentials_loader import try_load_credentials

            cred_status = try_load_credentials()
            if cred_status.ok and cred_status.credentials is not None:
                client = IGRestClient(cred_status.credentials)
                client.ensure_session()
                if hasattr(client, "flatten_all_positions"):
                    closed = int(client.flatten_all_positions())
            else:
                status["flatten"] = cred_status.error or "credentials unavailable"
        except Exception as e:
            status["flatten"] = f"{type(e).__name__}: {e}"
        else:
            status["flatten_closed"] = str(closed)

        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                f"🚨 COCKPIT EMERGENCY OVERRIDE — loops stopped, flattened={closed}"
            )
        except Exception:
            pass
    finally:
        with _flatten_lock:
            _inflight = False

    return status


def reset_emergency_override_for_tests() -> None:
    global _inflight
    with _flatten_lock:
        _inflight = False


def purge_cockpit_emergency_persistence() -> dict[str, Any]:
    """Force-clear COCKPIT_EMERGENCY_OVERRIDE from all known persistence layers."""
    status: dict[str, Any] = {"ok": True, "cockpit_emergency_override": False}
    paths_checked: list[str] = []

    try:
        from system.qmm_process_supervisor import clear_process_entry_block, process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked:
            clear_process_entry_block()
            status["process_entry_block"] = "cleared"
        elif detail:
            status["process_entry_block"] = "none"
    except Exception as exc:
        status["process_entry_block"] = f"{type(exc).__name__}: {exc}"

    try:
        from system.shutdown_cleanup import clear_manual_stop
        from system.paths import data_dir

        for rel in ("state/manual_stop.json",):
            p = data_dir() / rel
            paths_checked.append(str(p))
            if p.is_file():
                raw = p.read_text(encoding="utf-8")
                if "cockpit" in raw.lower():
                    clear_manual_stop()
                    status["manual_stop"] = "cleared"
        app_support = (
            Path.home()
            / "Library/Application Support/IG Agent Apex/v30-production/data/state/manual_stop.json"
        )
        paths_checked.append(str(app_support))
        if app_support.is_file():
            raw = app_support.read_text(encoding="utf-8")
            if "cockpit" in raw.lower():
                app_support.unlink(missing_ok=True)
                status["manual_stop_app_support"] = "cleared"
    except Exception as exc:
        status["manual_stop"] = f"{type(exc).__name__}: {exc}"

    try:
        from api.snapshot_store import get_tick, publish_tick

        tick = get_tick()
        if isinstance(tick, dict):
            br = str(tick.get("block_reason") or "")
            if "COCKPIT" in br.upper():
                patched = dict(tick)
                patched["block_reason"] = ""
                sig = patched.get("signal")
                if isinstance(sig, dict):
                    sig = dict(sig)
                    if "COCKPIT" in str(sig.get("block_reason") or "").upper():
                        sig["block_reason"] = ""
                    patched["signal"] = sig
                publish_tick(patched)
                status["snapshot_block_reason"] = "cleared"
    except Exception as exc:
        status["snapshot_block_reason"] = f"{type(exc).__name__}: {exc}"

    status["paths_checked"] = paths_checked
    log_engine("COCKPIT emergency persistence PURGED — override forced FALSE")
    return status


def clear_emergency_cockpit_override(*, resume_trading: bool = True) -> dict[str, Any]:
    """Release cockpit flatten hold — clears QMM block, manual stop, snapshot token."""
    purge_cockpit_emergency_persistence()
    status: dict[str, Any] = {"ok": True, "cockpit_emergency_override": False}

    with _flatten_lock:
        _inflight = False

    try:
        from system.qmm_process_supervisor import clear_process_entry_block

        clear_process_entry_block()
        status["process_entry_block"] = "cleared"
    except Exception as exc:
        status["process_entry_block"] = f"{type(exc).__name__}: {exc}"

    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
        status["manual_stop"] = "cleared"
    except Exception as exc:
        status["manual_stop"] = f"{type(exc).__name__}: {exc}"

    try:
        from api.snapshot_store import get_tick, publish_tick

        tick = get_tick()
        if isinstance(tick, dict):
            patched = dict(tick)
            patched["block_reason"] = ""
            sig = patched.get("signal")
            if isinstance(sig, dict):
                sig = dict(sig)
                sig["block_reason"] = ""
                patched["signal"] = sig
            publish_tick(patched)
            status["snapshot_block_reason"] = "cleared"
    except Exception as exc:
        status["snapshot_block_reason"] = f"{type(exc).__name__}: {exc}"

    if resume_trading:
        try:
            from api.agent_control import start_trading

            status["trading"] = start_trading()
        except Exception as exc:
            status["trading"] = f"{type(exc).__name__}: {exc}"

    log_engine("COCKPIT emergency override CLEARED — execution valve re-opened")
    return status
