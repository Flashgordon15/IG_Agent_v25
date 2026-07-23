"""Emergency bot kill — bypass normal loops: stop entries, cancel orders, flatten."""

from __future__ import annotations

import time
from typing import Any

from system.engine_log import log_engine


def run_emergency_kill(*, source: str = "api") -> dict[str, Any]:
    """
    Instant kill path:
      1. Mark emergency + manual stop (blocks watchdog restart)
      2. Stop trading loops
      3. Cancel all working orders
      4. Flatten all live broker positions via exit gate / REST
      5. Reconcile book
    """
    started = time.time()
    report: dict[str, Any] = {
        "ok": False,
        "source": source,
        "cancelled_orders": 0,
        "closed": [],
        "errors": [],
        "loops_stopped": False,
        "manual_stop": False,
    }

    try:
        from execution.exit_execution_gate import set_emergency_kill_active

        set_emergency_kill_active(True)
    except Exception as exc:
        report["errors"].append(f"kill_flag:{type(exc).__name__}:{exc}")

    try:
        from system.shutdown_cleanup import mark_manual_stop

        mark_manual_stop(source=f"emergency_kill:{source}")
        report["manual_stop"] = True
    except Exception as exc:
        report["errors"].append(f"manual_stop:{type(exc).__name__}:{exc}")

    try:
        from api.agent_control import stop_trading

        stop_res = stop_trading()
        report["loops_stopped"] = bool(stop_res.get("ok"))
        report["loops_status"] = stop_res.get("status") or stop_res.get("error")
    except Exception as exc:
        report["errors"].append(f"loops:{type(exc).__name__}:{exc}")

    rest = None
    try:
        from system.config_loader import load_active_config
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            raise RuntimeError(status.error or "credentials missing")
        cfg = load_active_config(validate=False)
        rest = ensure_shared_authenticated(status.credentials)
        report["cfg_ok"] = True
    except Exception as exc:
        report["errors"].append(f"rest:{type(exc).__name__}:{exc}")
        log_engine(f"EmergencyKill: REST unavailable — {exc}")
        report["elapsed_sec"] = round(time.time() - started, 2)
        return report

    try:
        if hasattr(rest, "cancel_all_working_orders"):
            report["cancelled_orders"] = int(rest.cancel_all_working_orders() or 0)
    except Exception as exc:
        report["errors"].append(f"cancel_orders:{type(exc).__name__}:{exc}")

    try:
        from execution.exit_execution_gate import request_flatten

        for item in list(rest.open_positions(budget_priority=True) or []):
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
            epic = str(mkt.get("epic") or "").strip()
            direction = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            if not deal_id or size <= 0:
                continue
            result = request_flatten(
                rest=rest,
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                reason="emergency_kill",
                pnl_gbp=None,
                cfg=cfg,
                source="emergency_kill",
            )
            if result.get("ok"):
                report["closed"].append(deal_id)
            else:
                report["errors"].append(
                    f"{deal_id}:{result.get('error') or result.get('reason') or 'fail'}"
                )
    except Exception as exc:
        report["errors"].append(f"flatten:{type(exc).__name__}:{exc}")
        # Last-resort REST bulk
        try:
            if hasattr(rest, "flatten_all_positions"):
                n = int(rest.flatten_all_positions() or 0)
                report["flatten_all_fallback"] = n
        except Exception as exc2:
            report["errors"].append(f"flatten_all:{type(exc2).__name__}:{exc2}")

    try:
        items = list(rest.open_positions(budget_priority=True) or [])
        report["broker_open_after"] = len(items)
        report["ok"] = len(items) == 0 and not report["errors"]
        if len(items) == 0:
            report["ok"] = True
    except Exception as exc:
        report["errors"].append(f"reconcile:{type(exc).__name__}:{exc}")

    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send(
                f"🛑 EMERGENCY KILL — closed={len(report['closed'])} "
                f"orders_cancelled={report['cancelled_orders']} "
                f"errors={len(report['errors'])}"
            )
    except Exception:
        pass

    report["elapsed_sec"] = round(time.time() - started, 2)
    log_engine(
        f"EmergencyKill: ok={report['ok']} closed={len(report['closed'])} "
        f"errors={len(report['errors'])} elapsed={report['elapsed_sec']}s"
    )
    return report
