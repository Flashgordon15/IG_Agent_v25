"""Trading-path readiness — truth for desk badge / ops_strip.

Feed health (quote age) is NOT trading-path live. This module answers:
can the AI actually place a DOW (hot-path) entry right now, and if not, why?
"""

from __future__ import annotations

from typing import Any

_DOW = "IX.D.DOW.IFM.IP"


def compute_trading_path_readiness(
    *,
    desk_idle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return trading_path_live + ordered blockers for UI / ops."""
    blockers: list[dict[str, str]] = []

    # Holds / pauses
    try:
        from runtime.deploy_hold import is_deploy_hold_active

        if is_deploy_hold_active():
            blockers.append(
                {"code": "deploy_hold", "label": "Deploy hold active — entries frozen"}
            )
    except Exception:
        pass
    try:
        from runtime.halt_sot import active_halt_flags, flag_file_active
        from system.paths import state_dir

        label_map = {
            "trading_paused.json": ("trading_paused", "Trading paused"),
            "entry_halt.json": ("entry_halt", "Entry halt active"),
            "offline_for_dev.json": ("offline_for_dev", "Offline for development"),
        }
        seen_codes: set[str] = set()
        for row in active_halt_flags(include_deploy_hold=False):
            name = str(row.get("name") or "")
            mapped = label_map.get(name)
            if not mapped:
                continue
            code, label = mapped
            if code in seen_codes:
                continue
            seen_codes.add(code)
            blockers.append(
                {
                    "code": code,
                    "label": f"{label}: {row.get('reason') or code}",
                }
            )
        # manual_stop stays on shared state_dir only (watchdog hold).
        manual = state_dir() / "manual_stop.json"
        if flag_file_active(manual):
            from runtime.halt_sot import read_flag_payload

            raw = read_flag_payload(manual)
            blockers.append(
                {
                    "code": "manual_stop",
                    "label": f"Manual stop / watchdog hold: {raw.get('reason') or 'manual_stop'}",
                }
            )
    except Exception:
        pass

    # Desk idle (slot / bars / gated) — caller should pass ops idle when available
    idle = desk_idle
    if isinstance(idle, dict) and idle.get("code"):
        code = str(idle.get("code"))
        blockers.append(
            {
                "code": f"desk_idle_{code}",
                "label": str(idle.get("label") or code),
            }
        )
    else:
        # Standalone path (accounting / tests) — resolve DOW regime gates directly
        try:
            from system.regime_state import get_regime_state_snapshot

            snap = get_regime_state_snapshot() or {}
            for m in snap.get("markets") or []:
                if not isinstance(m, dict):
                    continue
                if str(m.get("epic") or "") != _DOW:
                    continue
                gate = (
                    m.get("strategy_gate")
                    if isinstance(m.get("strategy_gate"), dict)
                    else {}
                )
                reason = str(m.get("reason") or "")
                if "insufficient" in reason.lower():
                    blockers.append(
                        {
                            "code": "desk_idle_insufficient_bars",
                            "label": "DOW warming — insufficient bars for regime",
                        }
                    )
                elif gate.get("allow_entries") is False:
                    blockers.append(
                        {
                            "code": "desk_idle_entries_gated",
                            "label": f"DOW entries gated ({gate.get('mode') or reason})",
                        }
                    )
                break
        except Exception:
            pass

    # Hot-path config authority — DOW must not be on exclude_from_hot_path.
    # Do NOT use epic_allowed_on_hot_path() here: that also requires an in-memory
    # active stack that can race empty on API reads and false-red the badge.
    try:
        from system.config_loader import get_config

        cfg = get_config()
        dual = cfg.get("dual_core") if hasattr(cfg, "get") else {}
        excluded = {
            str(e).strip()
            for e in ((dual or {}).get("exclude_from_hot_path") or [])
        }
        if _DOW in excluded:
            blockers.append(
                {
                    "code": "hot_path_excluded",
                    "label": "DOW excluded from hot path",
                }
            )
    except Exception:
        pass

    # Cap breach — snapshot SoT (never trust empty live under coalesce).
    try:
        import json

        from runtime.broker_snapshot import open_count_from_snapshot, read_snapshot
        from runtime.desk_stability_harness import boot_grace_active
        from system.config_loader import get_config
        from system.paths import data_dir

        cfg = get_config()
        max_open = max(1, int(getattr(cfg, "max_open_positions", 6) or 6))
        max_epic = max(1, int(getattr(cfg, "max_positions_per_epic", 2) or 2))
        snap_n = open_count_from_snapshot(max_age_sec=300.0)
        skip_stale_cap = False
        if boot_grace_active() and snap_n is not None and snap_n > max_open:
            try:
                ts_path = Path(data_dir()) / "trade_support_status.json"
                if ts_path.is_file():
                    raw_ts = json.loads(ts_path.read_text(encoding="utf-8"))
                    ts_open = int(raw_ts.get("broker_open") or 0)
                    skip_stale_cap = ts_open <= 0
                else:
                    skip_stale_cap = True
            except Exception:
                skip_stale_cap = True
        if snap_n is not None and snap_n > max_open and not skip_stale_cap:
            blockers.append(
                {
                    "code": "cap_breach",
                    "label": f"CAP BREACH broker_open={snap_n}>{max_open}",
                }
            )
        snap = read_snapshot(max_age_sec=300.0)
        if snap is not None:
            by_epic: dict[str, int] = {}
            for p in snap.get("positions") or []:
                ep = str(p.get("epic") or "")
                if ep:
                    by_epic[ep] = by_epic.get(ep, 0) + 1
            for ep, n in by_epic.items():
                if n > max_epic:
                    blockers.append(
                        {
                            "code": "epic_cap_breach",
                            "label": f"CAP BREACH {ep.split('.')[2] if '.' in ep else ep} {n}>{max_epic}",
                        }
                    )
                    break
    except Exception:
        pass

    # REST pressure — entries-only pause (reserve budget for closes).
    try:
        from system.rest_api_budget import entries_blocked_by_rest_pressure

        blocked, reason = entries_blocked_by_rest_pressure()
        if blocked:
            blockers.append(
                {
                    "code": "rest_pressure",
                    "label": f"REST PRESSURE — entries paused ({reason})",
                }
            )
    except Exception:
        pass

    # trade_support freshness — stale SoT means path cannot be live.
    try:
        import json
        import time
        from pathlib import Path

        from runtime.desk_stability_harness import trade_support_stale_budget_sec
        from system.paths import data_dir

        status_path = Path(data_dir()) / "trade_support_status.json"
        max_age = trade_support_stale_budget_sec()
        if status_path.is_file():
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            ts = float(raw.get("ts") or 0)
            age = time.time() - ts if ts > 0 else 9999.0
            if age > max_age:
                blockers.append(
                    {
                        "code": "trade_support_stale",
                        "label": (
                            f"trade_support SoT stale ({age:.0f}s>{max_age:.0f}s) "
                            f"— path down"
                        ),
                    }
                )
        else:
            blockers.append(
                {
                    "code": "trade_support_missing",
                    "label": "trade_support status missing — path down",
                }
            )
    except Exception:
        pass

    # Strategy controller must allow MICRO on the dual_core lane
    try:
        from runtime.strategy_controller import ExecutionPath, check_execution_permission

        perm = check_execution_permission(_DOW, ExecutionPath.MICRO)
        if not perm.allowed:
            blockers.append(
                {
                    "code": "strategy_controller_micro",
                    "label": str(
                        perm.reason
                        or "Strategy controller blocks MICRO on DOW"
                    ),
                }
            )
    except Exception:
        pass

    # Sniper ML is surfaced on ops_strip.sniper_ml — not a hard path-live veto.
    # Instant dual_core / MicroScalper can fire while sniper P is mid-pack; treating
    # sniper alone as DESK TRADING DOWN recreates the false-OK / false-DOWN lie.

    trade_ready = True
    try:
        from runtime.feed_health_watchdog import entries_blocked_by_feed_health

        if entries_blocked_by_feed_health():
            blockers.append(
                {
                    "code": "feed_health_entry_block",
                    "label": "Feed health blocking new entries",
                }
            )
    except Exception:
        pass
    # Prefer the same health_light derivation /api/health uses. Full
    # evaluate_iron_cage_readiness can lag false while the desk is already
    # dispatching — that recreates the false-red / false-green lie.
    try:
        from api.health_light import (
            get_health_light_response,
            iron_cage_from_health_light_snapshot,
        )

        cage = iron_cage_from_health_light_snapshot(get_health_light_response())
        trade_ready = bool(cage.get("trade_ready", True))
    except Exception:
        trade_ready = True
    if not trade_ready:
        blockers.append(
            {
                "code": "trade_ready_false",
                "label": "trade_ready=false (health_light iron cage)",
            }
        )

    # Deduplicate by code preserving order
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for b in blockers:
        c = b["code"]
        if c in seen:
            continue
        seen.add(c)
        uniq.append(b)

    live = len(uniq) == 0
    primary = uniq[0] if uniq else None
    return {
        "trading_path_live": live,
        "trade_ready": trade_ready,
        "hot_path_epic": _DOW,
        "blockers": uniq,
        "primary_blocker": primary,
        "badge": (
            "SYSTEM OPERATIONAL & TRADING PATH LIVE"
            if live
            else f"DESK TRADING DOWN — {(primary or {}).get('label') or 'ENTRY BLOCKED'}"
        ),
        "badge_code": "path_live" if live else (primary or {}).get("code") or "entry_blocked",
    }
