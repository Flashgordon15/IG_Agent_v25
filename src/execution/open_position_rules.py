"""
Unified open-position assessment — shared by in-process manager and CLI tools.

Evaluates broker P&L against micro_risk GBP budgets, trailing floors, and
portfolio caps. Does not execute closes; see open_position_actions.py.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpenPositionRow:
    deal_id: str
    epic: str
    direction: str
    size: float
    entry: float
    pnl_gbp: float | None = None
    loss_cap_gbp: float = 0.0
    soft_loss_gbp: float = 0.0
    target_gbp: float = 0.0
    trail_trigger_gbp: float = 0.0
    trail_floor_gbp: float = 0.0
    peak_profit_gbp: float = 0.0
    open_mins: float | None = None


@dataclass
class ManageAction:
    deal_id: str
    epic: str
    pnl_gbp: float
    action: str
    reason: str
    ok: bool = False
    error: str = ""


@dataclass
class ManageReport:
    broker_open: int = 0
    assessed: int = 0
    agent_up: bool = False
    source: str = ""
    sync_age_sec: float | None = None
    actions: list[ManageAction] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def position_management_cfg(cfg: Any | None) -> dict[str, Any]:
    try:
        block = getattr(cfg, "position_management", None) or {}
        if isinstance(block, dict):
            return dict(block)
    except Exception:
        pass
    return {}


def _profile_for(epic: str, size: float, cfg: Any) -> Any:
    from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

    _, _, prof = resolve_micro_tp_sl_for_epic(epic, size, cfg)
    return prof


def spread_adjusted_soft_loss_gbp(
    *,
    epic: str,
    size: float,
    soft_loss_gbp: float,
    loss_cap_gbp: float,
    trail_trigger_gbp: float = 0.0,
    peak_profit_gbp: float = 0.0,
    cfg: Any | None = None,
) -> float:
    """Widen soft_loss toward hard-cap ceiling when spread/chop would harvest it.

    Pre-trail-attach only. Never exceeds ``loss_cap * 0.90``. Hard cap unchanged.
    """
    _ = cfg
    base = max(0.25, float(soft_loss_gbp or 0.0))
    cap = max(base, float(loss_cap_gbp or base))
    hard_ceil = cap * 0.90
    # Once trail is armed, keep configured soft (micro trail owns giveback).
    if float(peak_profit_gbp or 0.0) >= max(0.25, float(trail_trigger_gbp or 0.0)):
        return base

    elast = 1.0
    spread_floor = base
    try:
        from execution.spread_elasticity import spread_elasticity_state
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(str(epic or ""))
        if snap is not None:
            bid = float(getattr(snap, "bid", 0) or 0)
            offer = float(getattr(snap, "offer", 0) or 0)
            if bid > 0 and offer > bid:
                elast = float(
                    spread_elasticity_state(str(epic), bid, offer).ratio or 1.0
                )
                # Spread cost in GBP ≈ spread_pts × size (£/pt for DOW)
                spread_pts = offer - bid
                spread_cost = abs(spread_pts) * max(0.01, float(size))
                # Soft must clear ≥1.25× one-way friction so chop spreads don't harvest
                spread_floor = max(base, min(hard_ceil, spread_cost * 1.25))
    except Exception:
        elast = 1.0

    chop = False
    try:
        from execution.pre_entry_regime_veto import _resolve_regime_label

        label = str(_resolve_regime_label(str(epic)) or "").upper()
        chop = label in (
            "CHOP",
            "RANGE_BOUND",
            "RANGE",
            "NEUTRAL",
            "STAGNANT_DZ",
            "STAGNANT",
            "MEAN_REVERSION",
        )
    except Exception:
        chop = False

    effective = max(base, spread_floor)
    if elast >= 1.45 or chop:
        # Give room toward hard ceiling so soft_loss isn't spread-harvested in chop
        pad = 0.82 if chop else 0.72
        effective = max(effective, hard_ceil * pad)
    return min(hard_ceil, effective)


def _risk_action_for_row(
    row: OpenPositionRow,
    *,
    gbp_track: dict[str, Any] | None,
    cfg: Any | None = None,
) -> ManageAction | None:
    if row.pnl_gbp is None:
        return None

    pnl = float(row.pnl_gbp)
    soft = float(row.soft_loss_gbp)
    cap = float(row.loss_cap_gbp)
    target = _effective_target_gbp_for_row(row, cfg) if cfg is not None else float(row.target_gbp)

    pm = position_management_cfg(cfg) if cfg is not None else {}

    peak = float(row.peak_profit_gbp)
    trail_trigger = float(row.trail_trigger_gbp)
    trail_floor = float(row.trail_floor_gbp)
    if gbp_track:
        peak = max(peak, float(gbp_track.get("peak_profit_gbp") or 0))
        trail_floor = max(trail_floor, float(gbp_track.get("trail_floor_gbp") or 0))
        trail_trigger = float(
            gbp_track.get("trail_trigger_gbp") or trail_trigger or 0
        )

    # v33 hot path — prefer SHM ring metrics when attached (dual-write SoT).
    try:
        from kernel.shm_facade import read_position

        shm = read_position(row.deal_id)
        if shm:
            trail_floor = max(
                trail_floor, float(shm.get("trail_floor_gbp") or 0.0)
            )
            soft = max(soft, float(shm.get("soft_loss_gbp") or 0.0))
            peak = max(peak, float(shm.get("peak_profit_gbp") or 0.0))
    except Exception:
        pass

    try:
        soft = spread_adjusted_soft_loss_gbp(
            epic=row.epic,
            size=row.size,
            soft_loss_gbp=soft,
            loss_cap_gbp=cap,
            trail_trigger_gbp=trail_trigger,
            peak_profit_gbp=peak,
            cfg=cfg,
        )
    except Exception:
        pass

    stagnant_sec = float(pm.get("stagnant_flatten_sec") or 0)
    stagnant_band = float(pm.get("stagnant_pnl_band_gbp") or 1.0)
    if (
        stagnant_sec > 0
        and row.open_mins is not None
        and row.open_mins * 60.0 >= stagnant_sec
        and -stagnant_band <= pnl <= stagnant_band
    ):
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=(
                f"stagnant_dead_zone pnl={pnl:.2f} "
                f"open_mins={row.open_mins:.1f} band=±{stagnant_band:.2f}"
            ),
        )

    if float(row.entry or 0) <= 0:
        return None

    if pnl <= -soft:
        hold_reason = ""
        try:
            from execution.loss_patience import (
                loss_patience_enabled,
                should_hold_losing_position,
            )

            if loss_patience_enabled(cfg):
                decision = should_hold_losing_position(
                    epic=row.epic,
                    direction=row.direction,
                    pnl_gbp=pnl,
                    soft_loss_gbp=soft,
                    loss_cap_gbp=cap,
                    open_mins=row.open_mins,
                    cfg=cfg,
                )
                if decision.hold:
                    hold_reason = decision.reason
        except Exception:
            hold_reason = ""
        if hold_reason:
            # Defer the soft-loss cut for mean reversion; the HARD loss cap below
            # is still enforced this same tick, so the loss can never run away.
            try:
                from system.engine_log import log_engine

                log_engine(
                    f"loss_patience HOLD {row.epic} deal={row.deal_id[:10]} "
                    f"pnl={pnl:.2f} soft=-{soft:.2f} cap=-{cap:.2f} — {hold_reason}"
                )
            except Exception:
                pass
        else:
            return ManageAction(
                deal_id=row.deal_id,
                epic=row.epic,
                pnl_gbp=pnl,
                action="flatten",
                reason=f"soft_loss breach {pnl:.2f} <= -{soft:.2f}",
            )
    if pnl <= -cap:
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=f"loss_cap breach {pnl:.2f} <= -{cap:.2f}",
        )

    if peak >= trail_trigger and trail_trigger > 0 and pnl <= trail_floor:
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=(
                f"trail_floor breach pnl={pnl:.2f} floor={trail_floor:.2f} "
                f"peak={peak:.2f}"
            ),
        )

    from execution.tiered_profit_banks import tiered_bank_reason

    try:
        from execution.profit_pct_tiers import pct_tier_bank_reason, pct_tiers_enabled

        if pct_tiers_enabled(cfg) and row.target_gbp > 0:
            pct_decision = pct_tier_bank_reason(
                peak=peak,
                pnl=pnl,
                target_gbp=row.target_gbp,
                epic=row.epic,
                direction=row.direction,
                trail_trigger_gbp=trail_trigger,
                armed_at=0.0,
                cfg=cfg,
            )
            if pct_decision is not None and not pct_decision.runner_extended:
                return ManageAction(
                    deal_id=row.deal_id,
                    epic=row.epic,
                    pnl_gbp=pnl,
                    action="flatten",
                    reason=pct_decision.reason,
                )
    except Exception:
        pass

    tier_reason = tiered_bank_reason(
        peak=peak,
        pnl=pnl,
        trail_trigger_gbp=trail_trigger,
        cfg=cfg,
    )
    if tier_reason:
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=tier_reason,
        )

    min_bank = float((gbp_track or {}).get("min_bank_win_gbp") or 1.0)
    quick_win_enabled = True
    if cfg is not None:
        mr = getattr(cfg, "micro_risk", None) or (
            cfg.get("micro_risk") if hasattr(cfg, "get") else None
        )
        if isinstance(mr, dict) and mr.get("quick_win_bank_enabled") is False:
            quick_win_enabled = False
    if (
        quick_win_enabled
        and peak >= min_bank
        and peak < trail_trigger
        and pnl <= peak * 0.50
        and pnl >= min_bank * 0.80
    ):
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=f"quick_win fade pnl={pnl:.2f} peak={peak:.2f}",
        )

    if pnl >= target:
        return ManageAction(
            deal_id=row.deal_id,
            epic=row.epic,
            pnl_gbp=pnl,
            action="flatten",
            reason=f"target hit {pnl:.2f} >= {target:.2f}",
        )
    return None


def _effective_target_gbp_for_row(row: OpenPositionRow, cfg: Any) -> float:
    try:
        from runtime.long_trade_runner import effective_target_gbp

        armed = 0.0
        if row.open_mins is not None:
            armed = time.time() - (row.open_mins * 60.0)
        return effective_target_gbp(
            loss_cap_gbp=row.loss_cap_gbp,
            base_target_gbp=row.target_gbp,
            armed_at=armed,
            peak_profit_gbp=row.peak_profit_gbp,
            trail_trigger_gbp=row.trail_trigger_gbp,
            cfg=cfg,
        )
    except Exception:
        return float(row.target_gbp)


def _cap_breach_actions(
    rows: list[OpenPositionRow],
    cfg: Any,
    *,
    enforce: bool,
) -> list[ManageAction]:
    if not enforce or not rows:
        return []

    from system.engine_lane import (
        ENGINE_CFD_SNIPER,
        ENGINE_SB_SENTINEL,
        count_cap_for_engine,
        infer_engine_id,
        resolve_active_engine_id,
    )

    max_epic = int(getattr(cfg, "max_positions_per_epic", 2) or 2)
    actions: list[ManageAction] = []
    flagged: set[str] = set()

    by_epic: dict[str, list[OpenPositionRow]] = defaultdict(list)
    for row in rows:
        by_epic[row.epic].append(row)

    for epic, plist in by_epic.items():
        if len(plist) <= max_epic:
            continue
        ranked = sorted(
            plist,
            key=lambda r: r.pnl_gbp if r.pnl_gbp is not None else 1e9,
        )
        for row in ranked[: len(plist) - max_epic]:
            if row.deal_id in flagged:
                continue
            flagged.add(row.deal_id)
            actions.append(
                ManageAction(
                    deal_id=row.deal_id,
                    epic=row.epic,
                    pnl_gbp=float(row.pnl_gbp or 0),
                    action="flatten",
                    reason=f"epic_cap breach {epic} ({len(plist)}>{max_epic})",
                )
            )

    remaining = [r for r in rows if r.deal_id not in flagged]
    active = resolve_active_engine_id(cfg)
    by_engine: dict[str, list[OpenPositionRow]] = defaultdict(list)
    for row in remaining:
        eid = infer_engine_id(cfg=cfg)
        if active == ENGINE_SB_SENTINEL:
            eid = ENGINE_SB_SENTINEL
        elif active == ENGINE_CFD_SNIPER:
            eid = ENGINE_CFD_SNIPER
        by_engine[eid].append(row)

    for engine_id, elist in by_engine.items():
        cap = count_cap_for_engine(engine_id, cfg)
        if cap is None or len(elist) <= cap:
            continue
        ranked = sorted(
            elist,
            key=lambda r: r.pnl_gbp if r.pnl_gbp is not None else 1e9,
        )
        for row in ranked[: len(elist) - cap]:
            if row.deal_id in flagged:
                continue
            flagged.add(row.deal_id)
            actions.append(
                ManageAction(
                    deal_id=row.deal_id,
                    epic=row.epic,
                    pnl_gbp=float(row.pnl_gbp or 0),
                    action="flatten",
                    reason=(
                        f"engine_cap breach {engine_id} "
                        f"({len(elist)}>{cap})"
                    ),
                )
            )
    return actions


def _age_breach_actions(
    rows: list[OpenPositionRow],
    max_age_minutes: float,
) -> list[ManageAction]:
    if max_age_minutes <= 0:
        return []
    actions: list[ManageAction] = []
    for row in rows:
        if row.open_mins is None or row.open_mins < max_age_minutes:
            continue
        actions.append(
            ManageAction(
                deal_id=row.deal_id,
                epic=row.epic,
                pnl_gbp=float(row.pnl_gbp or 0),
                action="flatten",
                reason=f"max_age {row.open_mins:.0f}m >= {max_age_minutes:.0f}m",
            )
        )
    return actions


def rows_from_ig_items(
    items: list[dict[str, Any]],
    cfg: Any,
    *,
    gbp_tracks: dict[str, Any] | None = None,
    open_mins_by_deal: dict[str, float] | None = None,
) -> list[OpenPositionRow]:
    from execution.position_pnl_gbp import pnl_gbp_from_ig_item
    from runtime.broker_snapshot import enrich_ig_items

    gbp_tracks = gbp_tracks or {}
    open_mins_by_deal = open_mins_by_deal or {}
    rows: list[OpenPositionRow] = []

    for item in enrich_ig_items(list(items or [])):
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        epic = str(mkt.get("epic") or "").strip()
        if not deal_id or not epic:
            continue
        try:
            size = float(pos.get("size") or 0)
            entry = float(pos.get("level") or 0)
        except (TypeError, ValueError):
            continue
        direction = str(pos.get("direction") or "BUY").upper()
        pnl = pnl_gbp_from_ig_item(item)
        # Air-gap fallback: raw broker UPL even when entry<=0 blocks mark math.
        if pnl is None:
            try:
                from execution.broker_upl_hard_floor import raw_broker_upl_gbp_from_ig_item

                pnl = raw_broker_upl_gbp_from_ig_item(item)
            except Exception:
                pnl = None
        prof = _profile_for(epic, size, cfg)
        loss_cap = float(prof.risk_per_trade_gbp)
        soft_loss = loss_cap * float(getattr(prof, "soft_loss_ratio", 0.55))
        target = loss_cap * float(prof.target_r_multiple)
        track = gbp_tracks.get(deal_id) or {}
        rows.append(
            OpenPositionRow(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                entry=entry,
                pnl_gbp=float(pnl) if pnl is not None else None,
                loss_cap_gbp=loss_cap,
                soft_loss_gbp=round(soft_loss, 2),
                target_gbp=target,
                trail_trigger_gbp=float(prof.trail_trigger_gbp),
                trail_floor_gbp=float(track.get("trail_floor_gbp") or 0),
                peak_profit_gbp=float(track.get("peak_profit_gbp") or 0),
                open_mins=open_mins_by_deal.get(deal_id),
            )
        )
    return rows


def rows_from_snapshot_positions(
    positions: list[dict[str, Any]],
    cfg: Any,
    *,
    gbp_tracks: dict[str, Any] | None = None,
    open_mins_by_deal: dict[str, float] | None = None,
) -> list[OpenPositionRow]:
    """Build rows from a shared broker snapshot (P&L already computed upstream).

    Used when a consumer reads ``runtime.broker_snapshot`` instead of polling IG
    directly. Hollow coalesce snapshots omit bid/offer — enrich from hub /
    fulfillment cache before treating a row as unvalued.
    """
    from runtime.broker_snapshot import enrich_snapshot_positions

    gbp_tracks = gbp_tracks or {}
    open_mins_by_deal = open_mins_by_deal or {}
    rows: list[OpenPositionRow] = []
    for p in enrich_snapshot_positions(list(positions or [])):
        deal_id = str(p.get("deal_id") or "").strip()
        epic = str(p.get("epic") or "").strip()
        if not deal_id or not epic:
            continue
        try:
            size = float(p.get("size") or 0)
            entry = float(p.get("entry") or 0)
        except (TypeError, ValueError):
            continue
        pnl = p.get("pnl_gbp")
        prof = _profile_for(epic, size, cfg)
        loss_cap = float(prof.risk_per_trade_gbp)
        soft_loss = loss_cap * float(getattr(prof, "soft_loss_ratio", 0.55))
        target = loss_cap * float(prof.target_r_multiple)
        track = gbp_tracks.get(deal_id) or {}
        rows.append(
            OpenPositionRow(
                deal_id=deal_id,
                epic=epic,
                direction=str(p.get("direction") or "BUY").upper(),
                size=size,
                entry=entry,
                pnl_gbp=float(pnl) if pnl is not None else None,
                loss_cap_gbp=loss_cap,
                soft_loss_gbp=round(soft_loss, 2),
                target_gbp=target,
                trail_trigger_gbp=float(prof.trail_trigger_gbp),
                trail_floor_gbp=float(track.get("trail_floor_gbp") or 0),
                peak_profit_gbp=float(track.get("peak_profit_gbp") or 0),
                open_mins=open_mins_by_deal.get(deal_id),
            )
        )
    return rows


def rows_from_sync_positions(
    positions: list[Any],
    cfg: Any,
    *,
    gbp_tracks: dict[str, Any] | None = None,
    sync: Any | None = None,
) -> list[OpenPositionRow]:
    from execution.position_pnl_gbp import pnl_gbp_for_open_row

    gbp_tracks = gbp_tracks or {}
    rows: list[OpenPositionRow] = []

    for p in positions:
        deal_id = str(getattr(p, "deal_id", "") or "").strip()
        epic = str(getattr(p, "epic", "") or "").strip()
        if not deal_id or not epic:
            continue
        try:
            size = float(getattr(p, "size", 0) or 0)
            entry = float(getattr(p, "level", 0) or 0)
        except (TypeError, ValueError):
            continue
        direction = str(getattr(p, "direction", "BUY") or "BUY").upper()
        upl = float(getattr(p, "upl", 0) or 0)
        pnl = pnl_gbp_for_open_row(
            epic=epic,
            direction=direction,
            entry_level=entry,
            size=size,
            upl=upl if abs(upl) >= 0.001 else None,
            bid=float(getattr(p, "bid", 0) or 0),
            offer=float(getattr(p, "offer", 0) or 0),
            currency=str(getattr(p, "currency", "") or ""),
        )
        prof = _profile_for(epic, size, cfg)
        loss_cap = float(prof.risk_per_trade_gbp)
        soft_loss = loss_cap * float(getattr(prof, "soft_loss_ratio", 0.55))
        target = loss_cap * float(prof.target_r_multiple)
        track = gbp_tracks.get(deal_id) or {}
        open_mins = None
        if sync is not None and hasattr(sync, "_open_mins_for_deal"):
            try:
                open_mins = sync._open_mins_for_deal(
                    deal_id, str(getattr(p, "created_at", "") or "")
                )
            except Exception:
                open_mins = None
        rows.append(
            OpenPositionRow(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                entry=entry,
                pnl_gbp=float(pnl) if pnl is not None else None,
                loss_cap_gbp=loss_cap,
                soft_loss_gbp=round(soft_loss, 2),
                target_gbp=target,
                trail_trigger_gbp=float(prof.trail_trigger_gbp),
                trail_floor_gbp=float(track.get("trail_floor_gbp") or 0),
                peak_profit_gbp=float(track.get("peak_profit_gbp") or 0),
                open_mins=open_mins,
            )
        )
    return rows


def assess_open_positions(
    rows: list[OpenPositionRow],
    cfg: Any,
    *,
    gbp_tracks: dict[str, Any] | None = None,
    agent_up: bool = False,
    source: str = "",
    sync_age_sec: float | None = None,
) -> ManageReport:
    pm = position_management_cfg(cfg)
    gbp_tracks = gbp_tracks or {}
    report = ManageReport(
        broker_open=len(rows),
        agent_up=agent_up,
        source=source,
        sync_age_sec=sync_age_sec,
    )

    seen_actions: set[str] = set()
    for row in rows:
        report.positions.append(
            {
                "deal_id": row.deal_id,
                "epic": row.epic,
                "direction": row.direction,
                "size": row.size,
                "entry": row.entry,
                "pnl_gbp": round(float(row.pnl_gbp), 2)
                if row.pnl_gbp is not None
                else None,
                "loss_cap_gbp": row.loss_cap_gbp,
                "soft_loss_gbp": row.soft_loss_gbp,
                "target_gbp": row.target_gbp,
                "trail_trigger_gbp": row.trail_trigger_gbp,
                "trail_floor_gbp": row.trail_floor_gbp,
                "peak_profit_gbp": row.peak_profit_gbp,
                "open_mins": row.open_mins,
            }
        )
        report.assessed += 1

        # B1 air-gap: absolute broker UPL hard floor — works even when entry<=0.
        try:
            from execution.broker_upl_hard_floor import _floor_cfg

            floor = float(_floor_cfg(cfg)["floor_gbp"])
            raw_upl = row.pnl_gbp
            if raw_upl is not None and float(raw_upl) <= floor:
                if row.deal_id not in seen_actions:
                    report.actions.append(
                        ManageAction(
                            deal_id=row.deal_id,
                            epic=row.epic,
                            pnl_gbp=float(raw_upl),
                            action="flatten",
                            reason=(
                                f"broker_upl_hard_floor {float(raw_upl):.2f} "
                                f"<= {floor:.2f}"
                            ),
                        )
                    )
                    seen_actions.add(row.deal_id)
                continue
            if raw_upl is None and float(row.entry or 0) <= 0:
                # Blind valuation — do NOT spam flatten. Only legacy opt-in
                # null_upl_fail_safe=true may close; default is log + skip.
                opts = _floor_cfg(cfg)
                if opts.get("enabled") and opts.get("null_upl_fail_safe"):
                    if row.deal_id not in seen_actions:
                        report.actions.append(
                            ManageAction(
                                deal_id=row.deal_id,
                                epic=row.epic,
                                pnl_gbp=floor,
                                action="flatten",
                                reason="broker_upl_null_fail_safe entry<=0",
                            )
                        )
                        seen_actions.add(row.deal_id)
                    continue
                report.issues.append(
                    f"{row.deal_id[:10]}: broker_upl_null_no_flatten "
                    "(no UPL/entry — skip hard-floor close)"
                )
                continue
        except Exception as exc:
            # Fail-SAFE on gate exception
            if row.deal_id not in seen_actions:
                report.actions.append(
                    ManageAction(
                        deal_id=row.deal_id,
                        epic=row.epic,
                        pnl_gbp=float(row.pnl_gbp)
                        if row.pnl_gbp is not None
                        else -100.0,
                        action="flatten",
                        reason=f"broker_upl_gate_exception:{type(exc).__name__}",
                    )
                )
                seen_actions.add(row.deal_id)
            continue

        if float(row.entry or 0) <= 0:
            report.issues.append(
                f"{row.deal_id[:10]}: untrusted_entry<=0 — soft/hard skipped "
                "(hard-floor gate evaluated)"
            )
            continue
        if row.pnl_gbp is None:
            report.issues.append(f"{row.deal_id[:10]}: no P&L (missing IG bid/offer)")
            continue

        act = _risk_action_for_row(
            row, gbp_track=gbp_tracks.get(row.deal_id), cfg=cfg
        )
        if act and act.deal_id not in seen_actions:
            report.actions.append(act)
            seen_actions.add(act.deal_id)

    if pm.get("enforce_cap_breach", True):
        for act in _cap_breach_actions(
            rows, cfg, enforce=True
        ):
            if act.deal_id not in seen_actions:
                report.actions.append(act)
                seen_actions.add(act.deal_id)

    max_age = float(pm.get("max_position_age_minutes") or 0)
    for act in _age_breach_actions(rows, max_age):
        if act.deal_id not in seen_actions:
            report.actions.append(act)
            seen_actions.add(act.deal_id)

    return report
