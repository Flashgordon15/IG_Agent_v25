"""Daily loss accounting — effective P&L after v29.1 baseline reset + soft pause."""

from __future__ import annotations

from datetime import date
from typing import Any

RUNTIME_VERSION_KEY = "daily_loss_reset_version"
RUNTIME_BASELINE_KEY = "daily_loss_baseline_pnl"
RUNTIME_DAY_KEY = "daily_loss_reset_day"
RUNTIME_AT_KEY = "daily_loss_reset_at"


def _today() -> str:
    return date.today().isoformat()


def daily_loss_reset_snapshot(store: Any | None) -> dict[str, Any]:
    if store is None:
        return {}
    try:
        return {
            "version": store.get_runtime_state(RUNTIME_VERSION_KEY),
            "baseline_pnl": store.get_runtime_state(RUNTIME_BASELINE_KEY),
            "reset_day": store.get_runtime_state(RUNTIME_DAY_KEY),
            "reset_at": store.get_runtime_state(RUNTIME_AT_KEY),
        }
    except Exception:
        return {}


def effective_daily_pnl(store: Any | None, *, day: str | None = None) -> float:
    """P&L for daily-loss gates — subtracts one-time baseline on reset day only."""
    if store is None:
        return 0.0
    d = day or _today()
    try:
        raw = float(store.sum_daily_pnl(d))
    except Exception:
        return 0.0
    reset_day = store.get_runtime_state(RUNTIME_DAY_KEY)
    if reset_day != d:
        return raw
    baseline_raw = store.get_runtime_state(RUNTIME_BASELINE_KEY)
    if baseline_raw is None:
        return raw
    try:
        baseline = float(baseline_raw)
    except (TypeError, ValueError):
        return raw
    return raw - baseline


def effective_daily_loss_gbp(store: Any | None, *, day: str | None = None) -> float:
    return max(0.0, -effective_daily_pnl(store, day=day))


def soft_pause_threshold_gbp(cfg: Any | None = None) -> float:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 400.0
    try:
        block = cfg.get("learning_demo_mode") or {}
        if isinstance(block, dict) and block.get("daily_loss_soft_pause_gbp") is not None:
            return float(block["daily_loss_soft_pause_gbp"])
    except (TypeError, ValueError, AttributeError):
        pass
    return 400.0


def hard_daily_loss_limit_gbp(cfg: Any | None = None) -> float:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 500.0
    try:
        return float(cfg.max_daily_loss_gbp)
    except (TypeError, ValueError, AttributeError):
        return 500.0


def daily_loss_gate_status(
    store: Any | None,
    cfg: Any | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Returns (passed, detail, meta) for points_state / risk gates."""
    loss = effective_daily_loss_gbp(store)
    hard = hard_daily_loss_limit_gbp(cfg)
    soft = soft_pause_threshold_gbp(cfg)
    raw_pnl = 0.0
    try:
        if store is not None:
            raw_pnl = float(store.sum_daily_pnl())
    except Exception:
        pass
    meta = {
        "effective_loss_gbp": round(loss, 2),
        "raw_daily_pnl": round(raw_pnl, 2),
        "soft_pause_gbp": soft,
        "hard_limit_gbp": hard,
        "reset": daily_loss_reset_snapshot(store),
    }
    if loss >= hard:
        return (
            False,
            f"daily loss £{loss:.2f} >= £{hard:.0f} (hard stop)",
            {**meta, "tier": "hard"},
        )
    if loss >= soft:
        return (
            False,
            f"soft pause — daily loss £{loss:.2f} >= £{soft:.0f} (entries blocked)",
            {**meta, "tier": "soft"},
        )
    return True, f"daily loss £{loss:.2f} < £{soft:.0f}", {**meta, "tier": "ok"}
