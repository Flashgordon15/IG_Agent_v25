"""Profit-run policy — UPL>=threshold → hold runners, skip hyper-trail.

Defaults CURRENT: policy inactive until ``profit_run.enabled`` is true.
Hard virtual/broker stops always remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _cfg_block(cfg: Any | None, key: str) -> dict[str, Any]:
    if cfg is None or not hasattr(cfg, "get"):
        return {}
    block = cfg.get(key) or {}
    return dict(block) if isinstance(block, dict) else {}


def profit_run_enabled(cfg: Any | None = None) -> bool:
    return bool(_cfg_block(cfg, "profit_run").get("enabled", False))


def profit_run_upl_threshold_gbp(cfg: Any | None = None) -> float:
    return float(_cfg_block(cfg, "profit_run").get("upl_threshold_gbp") or 15.0)


def profit_run_breakeven_offset_pts(cfg: Any | None = None) -> float:
    return float(_cfg_block(cfg, "profit_run").get("breakeven_offset_pts") or 1.0)


@dataclass(frozen=True)
class ProfitRunDecision:
    active: bool
    skip_hyper_trail: bool
    skip_dynamic_hyper: bool
    floor_to_breakeven_plus: bool
    breakeven_offset_pts: float
    keep_hard_stop: bool
    allow_long_runner_hold: bool
    reason: str


def evaluate_profit_run(
    *,
    unrealized_pnl_gbp: float | None,
    cfg: Any | None = None,
) -> ProfitRunDecision:
    """When UPL >= threshold and enabled: disable hyper-trail, floor BE+offset.

    Hard virtual/broker stop remains. long_trade_runner may continue hold.
    """
    thr = profit_run_upl_threshold_gbp(cfg)
    offset = profit_run_breakeven_offset_pts(cfg)
    try:
        upl = float(unrealized_pnl_gbp) if unrealized_pnl_gbp is not None else None
    except (TypeError, ValueError):
        upl = None
    if not profit_run_enabled(cfg):
        return ProfitRunDecision(
            active=False,
            skip_hyper_trail=False,
            skip_dynamic_hyper=False,
            floor_to_breakeven_plus=False,
            breakeven_offset_pts=offset,
            keep_hard_stop=True,
            allow_long_runner_hold=True,
            reason="profit_run_disabled",
        )
    if upl is None or upl < thr:
        return ProfitRunDecision(
            active=False,
            skip_hyper_trail=False,
            skip_dynamic_hyper=False,
            floor_to_breakeven_plus=False,
            breakeven_offset_pts=offset,
            keep_hard_stop=True,
            allow_long_runner_hold=True,
            reason=f"profit_run_below_threshold upl={upl}",
        )
    return ProfitRunDecision(
        active=True,
        skip_hyper_trail=True,
        skip_dynamic_hyper=True,
        floor_to_breakeven_plus=True,
        breakeven_offset_pts=offset,
        keep_hard_stop=True,
        allow_long_runner_hold=True,
        reason=f"profit_run_active upl={upl:.2f}>={thr:.2f}",
    )


def breakeven_plus_stop_level(
    *,
    direction: str,
    entry_level: float,
    offset_pts: float = 1.0,
) -> float:
    """Floor stop to entry ± offset (BUY → entry+offset, SELL → entry-offset)."""
    entry = float(entry_level)
    off = max(0.0, float(offset_pts))
    if str(direction or "").upper() == "SELL":
        return entry - off
    return entry + off


def should_skip_micro_gbp_hyper_trail(
    *,
    unrealized_pnl_gbp: float | None,
    cfg: Any | None = None,
) -> bool:
    return bool(evaluate_profit_run(unrealized_pnl_gbp=unrealized_pnl_gbp, cfg=cfg).skip_hyper_trail)


def should_skip_dynamic_limit_hyper(
    *,
    unrealized_pnl_gbp: float | None,
    cfg: Any | None = None,
) -> bool:
    return bool(evaluate_profit_run(unrealized_pnl_gbp=unrealized_pnl_gbp, cfg=cfg).skip_dynamic_hyper)
