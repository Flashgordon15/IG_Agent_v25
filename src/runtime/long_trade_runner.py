"""Long-trade runner — let profitable positions age with wider trails and higher targets."""

from __future__ import annotations

import time
from typing import Any


def _runner_block(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return {}
    block = getattr(cfg, "long_trade_runner", None)
    if block is None and hasattr(cfg, "get"):
        block = cfg.get("long_trade_runner")
    return dict(block) if isinstance(block, dict) else {}


def runner_enabled(cfg: Any | None = None) -> bool:
    return bool(_runner_block(cfg).get("enabled", True))


def position_age_sec(armed_at: float) -> float:
    if armed_at <= 0:
        return 0.0
    return max(0.0, time.time() - armed_at)


def is_long_runner_active(
    *,
    armed_at: float,
    peak_profit_gbp: float,
    trail_trigger_gbp: float,
    cfg: Any | None = None,
) -> bool:
    """True when trade has aged in profit — use relaxed exit profile."""
    block = _runner_block(cfg)
    if not block.get("enabled", True):
        return False
    min_age_sec = float(block.get("min_age_minutes") or 3.0) * 60.0
    if position_age_sec(armed_at) < min_age_sec:
        return False
    return float(peak_profit_gbp) >= float(trail_trigger_gbp)


def effective_target_gbp(
    *,
    loss_cap_gbp: float,
    base_target_gbp: float,
    armed_at: float,
    peak_profit_gbp: float,
    trail_trigger_gbp: float,
    cfg: Any | None = None,
) -> float:
    block = _runner_block(cfg)
    base = float(base_target_gbp)
    if not is_long_runner_active(
        armed_at=armed_at,
        peak_profit_gbp=peak_profit_gbp,
        trail_trigger_gbp=trail_trigger_gbp,
        cfg=cfg,
    ):
        return base
    ext_r = float(block.get("extended_target_r_multiple") or 4.0)
    extended = float(loss_cap_gbp) * ext_r
    return max(base, extended)


def effective_giveback_ratio(
    *,
    base_giveback: float,
    armed_at: float,
    peak_profit_gbp: float,
    trail_trigger_gbp: float,
    cfg: Any | None = None,
) -> float:
    if is_long_runner_active(
        armed_at=armed_at,
        peak_profit_gbp=peak_profit_gbp,
        trail_trigger_gbp=trail_trigger_gbp,
        cfg=cfg,
    ):
        block = _runner_block(cfg)
        return float(block.get("widened_giveback_ratio") or 0.40)
    return float(base_giveback)


def effective_lock_ratio(
    *,
    base_lock_ratio: float,
    peak: float,
    trail_trigger_gbp: float,
    armed_at: float,
    peak_profit_gbp: float,
    cfg: Any | None = None,
) -> float:
    """Progressive lock — relaxed cap when long runner is active."""
    ratio = float(base_lock_ratio)
    if peak > trail_trigger_gbp:
        bonus = min(0.14, (peak - trail_trigger_gbp) * 0.025)
        ratio = min(0.92, ratio + bonus)
    if is_long_runner_active(
        armed_at=armed_at,
        peak_profit_gbp=peak_profit_gbp,
        trail_trigger_gbp=trail_trigger_gbp,
        cfg=cfg,
    ):
        block = _runner_block(cfg)
        relaxed = float(block.get("relaxed_lock_ratio") or 0.65)
        ratio = min(ratio, relaxed)
    return max(0.4, min(0.92, ratio))


def skip_max_age_close_for_runner(
    *,
    side: str,
    entry: float,
    px: float,
    cfg: Any | None = None,
) -> bool:
    """Do not time-stop profitable runners — let trail/target manage exit."""
    block = _runner_block(cfg)
    if not block.get("enabled", True):
        return False
    if not block.get("skip_max_age_on_profit", True):
        return False
    direction = str(side or "BUY").upper()
    if direction == "BUY":
        return float(px) > float(entry)
    if direction == "SELL":
        return float(px) < float(entry)
    return False
