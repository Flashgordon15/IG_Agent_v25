"""Live canary session gate reset — isolate soak P&L from historical demo ledger."""

from __future__ import annotations

from typing import Any

from system.engine_log import log_engine


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    if hasattr(cfg, "get"):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def live_canary_enabled(cfg: Any | None = None) -> bool:
    if cfg is None:
        try:
            from system.config_loader import load_active_config

            cfg = load_active_config(validate=False)
        except Exception:
            return False
    lc = _cfg_get(cfg, "live_canary") or {}
    return isinstance(lc, dict) and bool(lc.get("enabled"))


def reset_live_canary_session_gates(
    store: Any,
    *,
    cfg: Any | None = None,
    points_engine: Any | None = None,
) -> dict[str, Any]:
    """
    On canary boot: baseline today's closed P&L and clear latched drawdown shield
    so Path A gates use the canary £5 envelope, not stale breach state.
    """
    if not live_canary_enabled(cfg):
        return {"applied": False, "reason": "live_canary.disabled"}
    if store is None:
        return {"applied": False, "reason": "store_unavailable"}

    from system.v291_upgrade import refresh_today_daily_loss_baseline
    from trading.manual_intervention import (
        SHIELD_BREACH_AT_KEY,
        SHIELD_BREACH_DAY_KEY,
        SHIELD_BREACH_KEY,
        SHIELD_LOSS_KEY,
    )

    baseline = refresh_today_daily_loss_baseline(
        store,
        cfg=cfg,
        points_engine=points_engine,
        version="v31-canary",
        reason="live_canary_boot",
    )
    for key in (
        SHIELD_BREACH_KEY,
        SHIELD_BREACH_DAY_KEY,
        SHIELD_BREACH_AT_KEY,
        SHIELD_LOSS_KEY,
    ):
        try:
            store.set_runtime_state(key, "")
        except Exception:
            pass

    log_engine(
        "live_canary: session baseline reset — "
        f"baseline_pnl={baseline.get('baseline_pnl')} "
        f"effective_loss_gbp={baseline.get('effective_loss_gbp')}"
    )
    return {"applied": True, **baseline}
