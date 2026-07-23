"""Position risk monitor — audit open vs armed tracks and strategy health."""

from __future__ import annotations

from typing import Any


def build_position_risk_report(*, rest: Any | None = None, cfg: Any | None = None) -> dict[str, Any]:
    from execution.micro_risk_profile import (
        loss_gbp_at_stop,
        resolve_micro_tp_sl_for_epic,
    )
    from execution.position_pnl_gbp import pnl_gbp_from_ig_item
    from execution.position_risk_stack import reconcile_open_positions_risk_stack
    from runtime.dynamic_limit_engine import snapshot as dyn_snap
    from runtime.micro_gbp_exit import snapshot as gbp_snap
    from runtime.virtual_stop_loss import virtual_stop_snapshot
    from system.config_loader import get_config

    if cfg is None:
        cfg = get_config()
    if rest is None:
        try:
            from runtime.trade_manager import get_dual_core_coordinator

            coord = get_dual_core_coordinator()
            rest = getattr(coord, "_rest", None) if coord else None
        except Exception:
            rest = None

    reconcile = (
        reconcile_open_positions_risk_stack(rest, cfg=cfg, force=False)
        if rest is not None
        else {}
    )

    # Prefer IG sync cache over live REST poll (avoids API timeouts under load).
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    broker_items: list[dict[str, Any]] = []
    if rest is not None:
        try:
            from runtime.agent_bootstrap import get_ig_position_sync

            sync = get_ig_position_sync()
            if sync is not None:
                snap = sync.snapshot()
                for p in snap.positions:
                    broker_items.append(
                        {
                            "position": {
                                "dealId": p.deal_id,
                                "direction": p.direction,
                                "size": p.size,
                                "level": p.level,
                                "stopLevel": getattr(p, "stop_level", 0),
                            },
                            "market": {
                                "epic": p.epic,
                                "bid": p.bid,
                                "offer": p.offer,
                                "currency": p.currency,
                            },
                        }
                    )
        except Exception:
            broker_items = []
        if not broker_items and hasattr(rest, "open_positions"):
            try:
                broker_items = list(rest.open_positions(budget_priority=True) or [])
            except Exception as exc:
                issues.append(f"broker poll failed: {type(exc).__name__}: {exc}")

    gbp_tracks = (gbp_snap().get("tracks") or {})
    stop_tracks = {
        str(p.get("deal_id") or p.get("track_id") or ""): p
        for p in (virtual_stop_snapshot().get("positions") or [])
    }
    dyn_tracks = (dyn_snap().get("tracks") or {})

    broker_open = 0

    if rest is not None and hasattr(rest, "open_positions"):
        try:
            for item in broker_items:
                pos = item.get("position") or {}
                mkt = item.get("market") or {}
                deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
                epic = str(mkt.get("epic") or "").strip()
                if not deal_id:
                    continue
                broker_open += 1
                try:
                    size = float(pos.get("size") or 0)
                    entry = float(pos.get("level") or 0)
                except (TypeError, ValueError):
                    size, entry = 0.0, 0.0
                direction = str(pos.get("direction") or "BUY").upper()
                pnl = pnl_gbp_from_ig_item(item)
                gbp_armed = deal_id in gbp_tracks
                v_armed = deal_id in stop_tracks
                d_armed = deal_id in dyn_tracks
                gbp_row = gbp_tracks.get(deal_id) or {}

                _, sl_pts, prof = resolve_micro_tp_sl_for_epic(epic, size, cfg)
                broker_loss_est = loss_gbp_at_stop(epic, size=size, stop_pts=sl_pts)

                row = {
                    "deal_id": deal_id,
                    "epic": epic,
                    "direction": direction,
                    "size": size,
                    "entry": entry,
                    "pnl_gbp": round(float(pnl), 2) if pnl is not None else None,
                    "gbp_armed": gbp_armed,
                    "virtual_armed": v_armed,
                    "dynamic_armed": d_armed,
                    "peak_profit_gbp": gbp_row.get("peak_profit_gbp"),
                    "trail_floor_gbp": gbp_row.get("trail_floor_gbp"),
                    "loss_cap_gbp": gbp_row.get("loss_cap_gbp"),
                    "target_gbp": gbp_row.get("target_profit_gbp"),
                    "broker_stop_loss_est_gbp": round(broker_loss_est, 2),
                }
                rows.append(row)

                if pnl is None:
                    issues.append(f"{deal_id[:10]}: P&L unavailable (no IG mark)")
                if not gbp_armed:
                    issues.append(f"{deal_id[:10]}: missing GBP exit track")
                if not v_armed:
                    issues.append(f"{deal_id[:10]}: missing virtual stop")
                if not d_armed:
                    issues.append(f"{deal_id[:10]}: missing dynamic trail")
                if broker_loss_est > float(prof.risk_per_trade_gbp) + 0.5:
                    issues.append(
                        f"{epic}: broker stop ~£{broker_loss_est:.0f} > "
                        f"software cap £{prof.risk_per_trade_gbp:.0f} (Gold min size)"
                    )
        except Exception as exc:
            issues.append(f"broker poll failed: {type(exc).__name__}: {exc}")

    stale_gbp = set(gbp_tracks) - {r["deal_id"] for r in rows}
    if stale_gbp:
        issues.append(f"stale GBP tracks ({len(stale_gbp)}) — pruned on reconcile")

    if broker_open > 15:
        issues.append(
            f"high open count ({broker_open}) — demo unlimited mode stacks correlated risk"
        )

    winners = [r for r in rows if (r.get("pnl_gbp") or 0) > 1.0]
    losers = [r for r in rows if (r.get("pnl_gbp") or 0) < -1.0]
    unmonitored = [r for r in rows if not r.get("gbp_armed")]

    verdict = "HEALTHY"
    if unmonitored or stale_gbp:
        verdict = "DEGRADED"
    if broker_open > 0 and len(issues) > broker_open:
        verdict = "AT_RISK"

    return {
        "ok": True,
        "verdict": verdict,
        "broker_open": broker_open,
        "gbp_tracks": len(gbp_tracks),
        "virtual_tracks": len(stop_tracks),
        "dynamic_tracks": len(dyn_tracks),
        "reconcile": reconcile,
        "winners": len(winners),
        "losers": len(losers),
        "unmonitored": len(unmonitored),
        "issues": issues[:40],
        "positions": rows[:60],
        "strategy": {
            "entry": "Core B micro-scalp (dual-core Z-band pierce, 10s cadence)",
            "active_stack": "DOW + Nikkei + Gold (3 slots)",
            "excluded_hot_path": "FTSE, DAX, FX, Crude — no new dual-core entries",
            "rotation_universe": "MarketOrchestrator Layer-3 matrix (longer horizon, feed-ranked)",
            "loss_cap_gbp": 4.0,
            "soft_loss_gbp": 2.2,
            "trail_from_gbp": 1.0,
            "min_bank_win_gbp": 0.75,
            "target_gbp": 8.0,
            "max_giveback": "28% from peak",
            "primary_exit": "GBP broker UPL watchdog (0.75s)",
            "backup_exit": "Virtual stop + dynamic limit on IG marks",
            "position_caps": "12 global / 4 per epic (demo throughput)",
            "known_risk": "Gold min size 10pt → broker stop can be ~£20 before software cut",
        },
    }
