"""v29.1 one-time upgrade — daily loss baseline reset so demo trading can resume."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from system.daily_loss_policy import (
    RUNTIME_AT_KEY,
    RUNTIME_BASELINE_KEY,
    RUNTIME_DAY_KEY,
    RUNTIME_VERSION_KEY,
    effective_daily_loss_gbp,
)
from system.engine_log import log_engine


def _reset_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return {}
    try:
        block = cfg.get("learning_demo_mode") or {}
        reset = block.get("daily_loss_reset") or {}
        return reset if isinstance(reset, dict) else {}
    except Exception:
        return {}


def refresh_today_daily_loss_baseline(
    store: Any,
    *,
    cfg: Any | None = None,
    points_engine: Any | None = None,
    version: str = "v29.1",
    reason: str = "startup",
) -> dict[str, Any]:
    """
    Archive today's raw closed P&L as the gate baseline so effective loss starts at £0.

    Trade rows in SQLite are untouched — only runtime_state offsets gate accounting.
    """
    reset_cfg = _reset_block(cfg)
    if not reset_cfg.get("enabled", True):
        return {"refreshed": False, "reason": "daily_loss_reset.disabled"}

    today = date.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        raw_pnl = float(store.sum_daily_pnl(today))
    except Exception:
        raw_pnl = 0.0

    store.set_runtime_state(RUNTIME_VERSION_KEY, str(version))
    store.set_runtime_state(RUNTIME_BASELINE_KEY, f"{raw_pnl:.4f}")
    store.set_runtime_state(RUNTIME_DAY_KEY, today)
    store.set_runtime_state(RUNTIME_AT_KEY, now)

    try:
        store.clear_circuit_breaker_state()
    except Exception as e:
        log_engine(
            f"daily_loss_reset: circuit breaker clear failed: {type(e).__name__}: {e}"
        )

    if points_engine is not None:
        try:
            if hasattr(points_engine, "clear_session_pause"):
                points_engine.clear_session_pause()
            elif hasattr(points_engine, "reset_session_pause"):
                points_engine.reset_session_pause()
        except Exception as e:
            log_engine(
                f"daily_loss_reset: points session pause clear: {type(e).__name__}: {e}"
            )

    effective_loss = effective_daily_loss_gbp(store)
    log_engine(
        f"DAILY_LOSS_RESET ({reason}) — baseline_pnl={raw_pnl:+.2f} day={today} "
        f"effective_loss_gbp={effective_loss:.2f} version={version} "
        f"(audit preserved; gates unblocked until new loss accumulates)"
    )
    return {
        "refreshed": True,
        "reason": reason,
        "version": version,
        "baseline_pnl": round(raw_pnl, 2),
        "effective_loss_gbp": round(effective_loss, 2),
        "reset_day": today,
        "reset_at": now,
    }


def apply_v291_upgrade(
    store: Any,
    *,
    cfg: Any | None = None,
    points_engine: Any | None = None,
) -> dict[str, Any]:
    """Apply v29.1 daily-loss baseline — first install or refresh on each startup."""
    reset_cfg = _reset_block(cfg)
    if not reset_cfg.get("enabled", True):
        return {"applied": False, "reason": "daily_loss_reset.disabled"}

    target = str(reset_cfg.get("upgrade_version") or "v29.1")
    current = store.get_runtime_state(RUNTIME_VERSION_KEY)
    refresh_on_startup = bool(reset_cfg.get("refresh_on_startup", True))

    if current == target and not refresh_on_startup:
        loss = effective_daily_loss_gbp(store)
        return {
            "applied": False,
            "reason": "already_applied",
            "version": target,
            "effective_loss_gbp": round(loss, 2),
        }

    first_install = current != target
    result = refresh_today_daily_loss_baseline(
        store,
        cfg=cfg,
        points_engine=points_engine,
        version=target,
        reason="v29.1_upgrade" if first_install else "startup_refresh",
    )
    if first_install:
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                f"✅ v29.1 upgrade — daily loss baseline reset. "
                f"Prior P&L £{result.get('baseline_pnl', 0):+.2f} archived; "
                f"effective loss now £{result.get('effective_loss_gbp', 0):.2f}. "
                f"Trading may resume."
            )
        except Exception:
            pass

    return {
        "applied": True,
        "refreshed": result.get("refreshed", True),
        "first_install": first_install,
        "version": target,
        "baseline_pnl": result.get("baseline_pnl"),
        "effective_loss_gbp": result.get("effective_loss_gbp"),
        "reset_day": result.get("reset_day"),
        "reset_at": result.get("reset_at"),
    }
