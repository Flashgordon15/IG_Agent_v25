"""Emergency cockpit override — flatten + halt execution routing."""

from __future__ import annotations

import threading

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
