"""
Built-in anti-zombie / supervision self-sanitization.

Embeds ``scripts/supervision_check.py --repair`` logic for bootstrap and E2E harness.
"""

from __future__ import annotations

from typing import Any


def run_supervision_self_sanitize(*, repair: bool = True) -> dict[str, Any]:
    """
    Evaluate launchd / watchdog drift and optionally repair.

    Non-blocking for Thread B — intended for pre-Gate1 bootstrap only.
    """
    from system.engine_log import log_engine
    from system.overnight_supervision import overnight_supervision_summary
    from system.supervision_monitor import (
        attempt_supervision_repair,
        evaluate_supervision_drift,
    )

    repairs: list[str] = []
    drift = evaluate_supervision_drift()
    summary = overnight_supervision_summary()

    if repair:
        try:
            from system.shutdown_cleanup import (
                agent_fully_stopped,
                ensure_supervision_utilities_executable,
                repair_stale_watchdog_after_stop,
            )

            util_ok, util_repaired = ensure_supervision_utilities_executable()
            if util_repaired:
                repairs.append(
                    "chmod +x " + ", ".join(util_repaired[:4])
                    + (" …" if len(util_repaired) > 4 else "")
                )
            elif not util_ok:
                repairs.append("supervision utilities still not executable")

            if not drift.get("ok"):
                _ok, detail = attempt_supervision_repair()
                repairs.append(detail)
                drift = evaluate_supervision_drift()
                summary = overnight_supervision_summary()

            stopped_ok, _ = agent_fully_stopped()
            if stopped_ok:
                wd_ok, wd_detail = repair_stale_watchdog_after_stop()
                if wd_detail and "no repair needed" not in wd_detail:
                    repairs.append(
                        f"watchdog cleanup: {'ok' if wd_ok else 'fail'} — {wd_detail}"
                    )
                    drift = evaluate_supervision_drift()
                    summary = overnight_supervision_summary()
        except Exception as exc:
            repairs.append(f"sanitize error: {type(exc).__name__}")

    payload = {
        "ok": bool(drift.get("ok")),
        "supervision_drift": drift,
        "overnight_supervision": summary,
        "repairs": repairs,
    }
    if repairs:
        log_engine(f"BootstrapSanitizer: repairs={'; '.join(repairs)}")
    else:
        log_engine(
            f"BootstrapSanitizer: drift_ok={payload['ok']} "
            f"watchdog={summary.get('launchd_watchdog')}"
        )
    return payload
