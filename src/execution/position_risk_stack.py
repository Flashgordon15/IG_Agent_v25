"""Arm + reconcile virtual stop, dynamic trail, and GBP exits for open contracts."""

from __future__ import annotations

import time
from typing import Any

from system.engine_log import log_engine
from system.pnl_math import price_delta_to_ig_points

_last_reconcile_at = 0.0
_last_forced_reconcile_at = 0.0
_RECONCILE_MIN_SEC = 12.0


def _synced_position_to_ig_item(p: Any) -> dict[str, Any]:
    return {
        "position": {
            "dealId": str(getattr(p, "deal_id", "") or ""),
            "epic": str(getattr(p, "epic", "") or ""),
            "direction": str(getattr(p, "direction", "BUY") or "BUY"),
            "size": float(getattr(p, "size", 0) or 0),
            "level": float(getattr(p, "level", 0) or 0),
            "stopLevel": float(getattr(p, "stop_level", 0) or 0),
        },
        "market": {
            "epic": str(getattr(p, "epic", "") or ""),
            "bid": float(getattr(p, "bid", 0) or 0),
            "offer": float(getattr(p, "offer", 0) or 0),
            "currency": str(getattr(p, "currency", "") or ""),
        },
    }


def _load_open_position_items(
    rest_client: Any,
    *,
    budget_priority: bool = False,
) -> list[dict[str, Any]]:
    """Prefer fresh IgPositionSync cache; shared snapshot; REST fallback."""
    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            if not sync.is_fresh():
                try:
                    sync.request_refresh()
                except Exception:
                    pass
            positions = list(sync.snapshot().positions or [])
            if positions and sync.is_fresh():
                return [_synced_position_to_ig_item(p) for p in positions]
    except Exception:
        pass
    try:
        from runtime import broker_snapshot

        shared = broker_snapshot.read_snapshot(max_age_sec=12.0)
        if shared and shared.get("positions"):
            items: list[dict[str, Any]] = []
            for p in shared.get("positions") or []:
                deal_id = str(p.get("deal_id") or "").strip()
                epic = str(p.get("epic") or "").strip()
                if not deal_id or not epic:
                    continue
                items.append(
                    {
                        "position": {
                            "dealId": deal_id,
                            "epic": epic,
                            "direction": str(p.get("direction") or "BUY"),
                            "size": float(p.get("size") or 0),
                            "level": float(p.get("entry") or 0),
                            "stopLevel": 0.0,
                        },
                        "market": {"epic": epic},
                    }
                )
            if items:
                return items
    except Exception:
        pass
    return list(rest_client.open_positions(budget_priority=budget_priority) or [])


def _position_management_interval(cfg: Any | None) -> float:
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        block = cfg.get("position_management") if hasattr(cfg, "get") else {}
        if not isinstance(block, dict):
            block = {}
        return max(10.0, float(block.get("reconcile_interval_sec", 15.0)))
    except Exception:
        return 15.0


def prune_orphaned_risk_tracks() -> int:
    """Remove GBP/virtual/dynamic tracks when broker is flat."""
    from runtime.dynamic_limit_engine import remove_track as remove_dyn
    from runtime.dynamic_limit_engine import snapshot as dyn_snap
    from runtime.micro_gbp_exit import remove_track as remove_gbp
    from runtime.micro_gbp_exit import snapshot as gbp_snap
    from runtime.virtual_stop_loss import clear_virtual_stop
    from runtime.virtual_stop_loss import virtual_stop_snapshot

    gbp_ids = set((gbp_snap().get("tracks") or {}).keys())
    stop_ids = {
        str(p.get("deal_id") or p.get("track_id") or "")
        for p in (virtual_stop_snapshot().get("positions") or [])
    }
    dyn_ids = set((dyn_snap().get("tracks") or {}).keys())
    all_ids = gbp_ids | stop_ids | dyn_ids
    pruned = 0
    for stale_id in all_ids:
        if not stale_id:
            continue
        remove_gbp(stale_id)
        clear_virtual_stop(stale_id)
        remove_dyn(stale_id)
        pruned += 1
    if pruned:
        log_engine(f"position_risk_stack: pruned {pruned} orphaned track(s) (broker flat)")
    return pruned


def ensure_risk_stack_coverage(
    rest_client: Any | None,
    cfg: Any | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    """
    Periodic watchdog hook — force reconcile when interval elapsed or track/open mismatch.
    """
    global _last_forced_reconcile_at
    if rest_client is None:
        return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": 0}

    now = time.time()
    interval = _position_management_interval(cfg)
    due = force or (now - _last_forced_reconcile_at >= interval)

    if not due:
        return reconcile_open_positions_risk_stack(rest_client, cfg=cfg, force=False)

    try:
        from runtime.dynamic_limit_engine import snapshot as dyn_snap
        from runtime.micro_gbp_exit import snapshot as gbp_snap
        from runtime.virtual_stop_loss import virtual_stop_snapshot

        gbp_n = len(gbp_snap().get("tracks") or {})
        stop_n = len(virtual_stop_snapshot().get("positions") or [])
        dyn_n = len(dyn_snap().get("tracks") or {})
        broker_n = 0
        if hasattr(rest_client, "open_positions"):
            try:
                broker_n = len(rest_client.open_positions() or [])
            except Exception:
                broker_n = -1
        if broker_n == 0:
            pruned = prune_orphaned_risk_tracks()
            return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": pruned}
        if broker_n > 0 and (gbp_n == 0 or stop_n == 0 or dyn_n == 0):
            force = True
        if broker_n >= 0 and (
            gbp_n != broker_n or stop_n != broker_n or dyn_n != broker_n
        ):
            force = True
    except Exception:
        force = True

    if force:
        _last_forced_reconcile_at = now
    return reconcile_open_positions_risk_stack(rest_client, cfg=cfg, force=force)


def _broker_stop_pts(
    *,
    epic: str,
    entry_level: float,
    stop_level: float,
) -> float:
    if entry_level <= 0 or stop_level <= 0:
        return 4.0
    return max(
        0.5,
        price_delta_to_ig_points(epic, abs(float(entry_level) - float(stop_level))),
    )


def arm_position_risk_stack(
    *,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    deal_id: str,
    stop_distance_pts: float,
    limit_distance_pts: float | None = None,
    cfg: Any | None = None,
    rest_client: Any | None = None,
) -> dict[str, Any]:
    """Single entry point for new fills — GBP + virtual stop + dynamic limit."""
    from execution.micro_risk_profile import omit_broker_limit_at_entry
    from execution.position_risk_arms import (
        arm_dynamic_limit_for_position,
        arm_gbp_exit_for_position,
        arm_virtual_stop_for_position,
        bind_risk_rest_clients,
    )

    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    bind_risk_rest_clients(rest_client)
    omit_limit = omit_broker_limit_at_entry(cfg)
    limit = None if omit_limit else limit_distance_pts

    arm_gbp_exit_for_position(
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        entry_level=entry_level,
        cfg=cfg,
    )
    arm_virtual_stop_for_position(
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        entry_level=entry_level,
        broker_stop_pts=float(stop_distance_pts),
        cfg=cfg,
    )
    arm_dynamic_limit_for_position(
        deal_id=deal_id,
        epic=epic,
        direction=direction,
        size=size,
        entry_level=entry_level,
        broker_stop_pts=float(stop_distance_pts),
        limit_distance_pts=limit,
        cfg=cfg,
        rest_client=rest_client,
    )

    try:
        from runtime.trade_lifecycle import LifecycleState, transition

        transition(
            deal_id,
            LifecycleState.TRAILING_STOP_ACTIVE,
            message="Risk stack armed (post-fill)",
            extra={"entry_level": entry_level},
        )
    except Exception:
        pass

    log_engine(
        f"position_risk_stack: armed deal={deal_id} epic={epic} "
        f"entry={entry_level} broker_stop={float(stop_distance_pts):.2f}pt"
    )
    return {"ok": True, "deal_id": deal_id}


def reconcile_open_positions_risk_stack(
    rest_client: Any | None,
    cfg: Any | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    """
    Ensure every IG open position has GBP exit + virtual stop + dynamic trail armed.

    Runs at most every 12s unless force=True. Prunes stale tracks for closed deals.
    """
    global _last_reconcile_at
    now = time.time()
    if not force and now - _last_reconcile_at < _RECONCILE_MIN_SEC:
        return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": 0, "throttled": 1}
    _last_reconcile_at = now

    if rest_client is None or not hasattr(rest_client, "open_positions"):
        return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": 0}

    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    from execution.micro_risk_profile import omit_broker_limit_at_entry
    from execution.position_risk_arms import (
        arm_dynamic_limit_for_position,
        arm_gbp_exit_for_position,
        arm_virtual_stop_for_position,
        bind_risk_rest_clients,
    )
    from runtime.dynamic_limit_engine import remove_track as remove_dyn
    from runtime.dynamic_limit_engine import snapshot as dyn_snap
    from runtime.micro_gbp_exit import remove_track as remove_gbp
    from runtime.micro_gbp_exit import snapshot as gbp_snap
    from runtime.virtual_stop_loss import clear_virtual_stop
    from runtime.virtual_stop_loss import virtual_stop_snapshot

    bind_risk_rest_clients(rest_client)
    omit_limit = omit_broker_limit_at_entry(cfg)

    gbp_ids = set((gbp_snap().get("tracks") or {}).keys())
    stop_ids = {
        str(p.get("deal_id") or p.get("track_id") or "")
        for p in (virtual_stop_snapshot().get("positions") or [])
    }
    dyn_ids = set((dyn_snap().get("tracks") or {}).keys())

    armed = 0
    gbp_only = 0
    skipped = 0
    pruned = 0

    try:
        rows = _load_open_position_items(rest_client, budget_priority=False)
    except Exception as exc:
        # Under REST pressure prefer last-good shared snapshot and KEEP arming.
        try:
            from runtime.broker_snapshot import ig_items_from_snapshot, read_snapshot

            snap = read_snapshot(max_age_sec=None)
            rows = ig_items_from_snapshot(snap, max_age_sec=None)
            if snap is not None:
                log_engine(
                    f"position_risk_stack: coalesce to snapshot after "
                    f"{type(exc).__name__} (count={snap.get('count')})"
                )
            if not rows and snap is not None and int(snap.get("count") or 0) == 0:
                pruned = prune_orphaned_risk_tracks()
                return {
                    "armed": 0,
                    "gbp": 0,
                    "skipped": 0,
                    "pruned": pruned,
                    "open": 0,
                    "coalesced": True,
                }
            if not rows:
                return {
                    "armed": 0,
                    "gbp": 0,
                    "skipped": 0,
                    "pruned": 0,
                    "open": int((snap or {}).get("count") or 0),
                    "coalesced": True,
                }
        except Exception:
            log_engine(
                f"position_risk_stack: reconcile failed: {type(exc).__name__}: {exc}"
            )
            return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": 0}

    if not rows:
        pruned = prune_orphaned_risk_tracks()
        return {"armed": 0, "gbp": 0, "skipped": 0, "pruned": pruned, "open": 0}

    open_ids: set[str] = set()

    for item in rows:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
        epic = str(mkt.get("epic") or pos.get("epic") or "").strip()
        if not deal_id or not epic:
            skipped += 1
            continue
        try:
            size = float(pos.get("size") or 0)
            entry = float(pos.get("level") or pos.get("openLevel") or 0)
            stop_level = float(pos.get("stopLevel") or 0)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if size <= 0 or entry <= 0:
            skipped += 1
            continue

        open_ids.add(deal_id)
        direction = str(pos.get("direction") or "BUY").upper()
        broker_stop = _broker_stop_pts(epic=epic, entry_level=entry, stop_level=stop_level)
        limit_arg = None if omit_limit else None

        if deal_id not in gbp_ids:
            arm_gbp_exit_for_position(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                entry_level=entry,
                cfg=cfg,
            )
            gbp_only += 1
            gbp_ids.add(deal_id)

        if deal_id not in stop_ids:
            arm_virtual_stop_for_position(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                entry_level=entry,
                broker_stop_pts=broker_stop,
                cfg=cfg,
            )
            stop_ids.add(deal_id)
            armed += 1

        if deal_id not in dyn_ids:
            arm_dynamic_limit_for_position(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
                entry_level=entry,
                broker_stop_pts=broker_stop,
                limit_distance_pts=limit_arg,
                cfg=cfg,
                rest_client=rest_client,
            )
            dyn_ids.add(deal_id)
            armed += 1

    for stale in gbp_ids - open_ids:
        remove_gbp(stale)
        clear_virtual_stop(stale)
        remove_dyn(stale)
        pruned += 1

    if armed or gbp_only or pruned:
        log_engine(
            f"position_risk_stack: reconciled full={armed} gbp_new={gbp_only} "
            f"pruned={pruned} skipped={skipped} open={len(open_ids)}"
        )
    return {
        "armed": armed,
        "gbp": gbp_only,
        "skipped": skipped,
        "pruned": pruned,
        "open": len(open_ids),
    }
