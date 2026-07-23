"""Shared helpers for arming per-position risk controls."""

from __future__ import annotations

from typing import Any


def arm_gbp_exit_for_position(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    cfg: Any | None = None,
) -> None:
    from execution.micro_risk_profile import (
        loss_gbp_at_stop,
        resolve_micro_tp_sl_for_epic,
    )
    from runtime.micro_gbp_exit import register_gbp_exit

    _, _, profile = resolve_micro_tp_sl_for_epic(epic, size, cfg)
    loss_cap = float(profile.risk_per_trade_gbp)
    soft_loss = loss_cap * float(profile.soft_loss_ratio)
    target_mult = 1.0
    try:
        from runtime.parameter_tuner import get_profit_target_multiplier
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        snap = get_regime_switch_snapshot()
        markets = snap.get("markets") or []
        regime = 0
        if markets:
            regime = int(markets[0].get("regime_state", markets[0].get("state", 0)) or 0)
        target_mult = float(get_profit_target_multiplier(regime))
    except Exception:
        pass
    target = loss_cap * float(profile.target_r_multiple) * target_mult
    trail_gbp = float(profile.trail_trigger_gbp)
    if trail_gbp <= 0:
        trail_gbp = max(
            float(profile.min_bank_win_gbp),
            loss_gbp_at_stop(epic, size=size, stop_pts=float(profile.trail_trigger_pts))
            * 0.35,
        )
    register_gbp_exit(
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        entry_level=entry_level,
        loss_cap_gbp=loss_cap,
        soft_loss_gbp=soft_loss,
        target_profit_gbp=target,
        trail_trigger_gbp=max(0.75, trail_gbp),
        trail_lock_ratio=float(profile.trail_profit_lock_ratio),
        min_bank_win_gbp=float(profile.min_bank_win_gbp),
        max_giveback_ratio=float(profile.max_giveback_ratio),
    )


def arm_virtual_stop_for_position(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    broker_stop_pts: float,
    cfg: Any | None = None,
) -> None:
    from execution.micro_risk_profile import (
        resolve_micro_tp_sl_for_epic,
        resolve_virtual_ceiling_pts,
    )
    from runtime.virtual_stop_loss import register_virtual_stop

    _, _, profile = resolve_micro_tp_sl_for_epic(epic, size, cfg)
    ceiling = resolve_virtual_ceiling_pts(
        epic=epic,
        broker_stop_pts=float(broker_stop_pts),
        profile=profile,
    )
    register_virtual_stop(
        epic=epic,
        direction=direction,
        entry_level=float(entry_level),
        size=float(size),
        deal_id=deal_id,
        ceiling_pts=ceiling,
    )


def arm_dynamic_limit_for_position(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    broker_stop_pts: float,
    limit_distance_pts: float | None,
    cfg: Any | None = None,
    rest_client: Any | None = None,
) -> None:
    from execution.micro_risk_profile import (
        omit_broker_limit_at_entry,
        resolve_micro_tp_sl_for_epic,
    )
    from runtime.dynamic_limit_engine import bind_rest_client, register_dynamic_limit

    tp_pts, _, profile = resolve_micro_tp_sl_for_epic(epic, size, cfg)
    limit_pts = float(limit_distance_pts or 0.0)
    if limit_pts <= 0:
        limit_pts = float(tp_pts)
    if rest_client is not None:
        bind_rest_client(rest_client)
    register_dynamic_limit(
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        entry_level=float(entry_level),
        limit_pts=limit_pts,
        size=float(size),
        trail_trigger_ig_pts=float(profile.trail_trigger_pts),
        trail_lock_ratio=float(profile.trail_profit_lock_ratio),
    )


def bind_risk_rest_clients(rest_client: Any | None) -> None:
    if rest_client is None:
        return
    try:
        from runtime.dynamic_limit_engine import bind_rest_client as bind_dyn
        from runtime.micro_gbp_exit import bind_rest_client as bind_gbp

        bind_dyn(rest_client)
        bind_gbp(rest_client)
    except Exception:
        pass
