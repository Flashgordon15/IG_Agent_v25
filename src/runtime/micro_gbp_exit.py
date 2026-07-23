"""
GBP P&L exits for micro scalps — broker-authoritative, hub-scale agnostic.

Yahoo hub mids (~100) diverge from IG spreadbet fill prices (~52k / ~3.3k).
Price-based virtual stops never fire; broker wide stops clip at ~±£20 on Gold.
This engine polls IG open-position UPL and enforces the micro_risk GBP budget.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine

_lock = threading.RLock()
_tracks: dict[str, "GbpExitTrack"] = {}
_in_flight: set[str] = set()
_rest_client: Any | None = None
_active = False
_last_poll_at = 0.0
_POLL_MIN_SEC = 0.35


@dataclass
class GbpExitTrack:
    deal_id: str
    epic: str
    direction: str
    size: float
    entry_level: float
    loss_cap_gbp: float
    soft_loss_gbp: float
    target_profit_gbp: float
    trail_trigger_gbp: float
    trail_lock_ratio: float
    min_bank_win_gbp: float = 1.0
    max_giveback_ratio: float = 0.30
    peak_profit_gbp: float = 0.0
    trail_floor_gbp: float = 0.0
    armed_at: float = 0.0


def start_micro_gbp_exit_engine(rest_client: Any | None = None) -> None:
    global _active, _rest_client, _POLL_MIN_SEC
    _active = True
    if rest_client is not None:
        _rest_client = rest_client
    try:
        from execution.micro_risk_profile import _load_profile
        from system.config_loader import get_config

        prof = _load_profile(get_config())
        _POLL_MIN_SEC = max(0.2, float(prof.gbp_poll_sec))
    except Exception:
        pass
    log_engine(
        f"MicroGbpExit: engine active (broker UPL watchdog poll={_POLL_MIN_SEC:.2f}s)"
    )


def bind_rest_client(rest_client: Any | None) -> None:
    global _rest_client
    if rest_client is not None:
        _rest_client = rest_client


def register_gbp_exit(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    size: float,
    entry_level: float,
    loss_cap_gbp: float,
    target_profit_gbp: float,
    trail_trigger_gbp: float,
    trail_lock_ratio: float = 0.70,
    min_bank_win_gbp: float = 1.0,
    soft_loss_gbp: float | None = None,
    max_giveback_ratio: float = 0.30,
) -> None:
    key = str(deal_id or "").strip()
    if not key:
        return
    cap = max(0.5, float(loss_cap_gbp))
    soft = float(soft_loss_gbp) if soft_loss_gbp is not None else cap * 0.55
    soft = min(soft, cap * 0.95)
    with _lock:
        prev = _tracks.get(key)
        peak = float(prev.peak_profit_gbp) if prev is not None else 0.0
        floor = float(prev.trail_floor_gbp) if prev is not None else 0.0
        track = GbpExitTrack(
            deal_id=key,
            epic=str(epic or "").strip(),
            direction=str(direction or "BUY").upper(),
            size=max(0.01, float(size)),
            entry_level=float(entry_level),
            loss_cap_gbp=cap,
            soft_loss_gbp=max(0.25, soft),
            target_profit_gbp=max(0.5, float(target_profit_gbp)),
            trail_trigger_gbp=max(0.25, float(trail_trigger_gbp)),
            trail_lock_ratio=max(0.4, min(0.95, float(trail_lock_ratio))),
            min_bank_win_gbp=max(0.25, float(min_bank_win_gbp)),
            max_giveback_ratio=max(0.1, min(0.5, float(max_giveback_ratio))),
            peak_profit_gbp=peak,
            trail_floor_gbp=floor,
            armed_at=time.time(),
        )
        _tracks[key] = track
    _publish_track_to_shm(track)


def _publish_track_to_shm(track: GbpExitTrack, *, pnl_gbp: float | None = None) -> None:
    try:
        from kernel.shm_facade import publish_position_risk

        publish_position_risk(
            deal_id=track.deal_id,
            epic=track.epic,
            soft_loss_gbp=track.soft_loss_gbp,
            trail_floor_gbp=track.trail_floor_gbp,
            atr_limit_gbp=track.target_profit_gbp,
            pnl_gbp=pnl_gbp,
            peak_profit_gbp=track.peak_profit_gbp,
        )
    except Exception:
        pass


def remove_track(deal_id: str) -> None:
    with _lock:
        _tracks.pop(str(deal_id or "").strip(), None)
        _in_flight.discard(str(deal_id or "").strip())


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "active": _active,
            "tracks": {
                k: {
                    "epic": v.epic,
                    "peak_profit_gbp": v.peak_profit_gbp,
                    "trail_floor_gbp": v.trail_floor_gbp,
                    "loss_cap_gbp": v.loss_cap_gbp,
                    "soft_loss_gbp": v.soft_loss_gbp,
                    "target_profit_gbp": v.target_profit_gbp,
                    "trail_trigger_gbp": v.trail_trigger_gbp,
                    "min_bank_win_gbp": v.min_bank_win_gbp,
                    "max_giveback_ratio": v.max_giveback_ratio,
                }
                for k, v in _tracks.items()
            },
        }


def reset_micro_gbp_exit_for_tests() -> None:
    global _active, _rest_client, _last_poll_at, _POLL_MIN_SEC
    with _lock:
        _tracks.clear()
        _in_flight.clear()
        _active = False
        _last_poll_at = 0.0
    _rest_client = None
    _POLL_MIN_SEC = 0.35


def on_watchdog_tick() -> None:
    """Called from virtual-stop watchdog (~500ms) — broker UPL poll."""
    global _last_poll_at
    if not _active:
        return
    with _lock:
        if not _tracks:
            return
    now = time.time()
    if now - _last_poll_at < _POLL_MIN_SEC:
        return
    _last_poll_at = now
    pnls = _load_broker_pnls_gbp()
    if not pnls:
        return
    open_ids = set(pnls.keys())
    with _lock:
        tracks = list(_tracks.values())
    for track in tracks:
        if track.deal_id in _in_flight:
            continue
        pnl = pnls.get(track.deal_id)
        if pnl is None:
            pnl = _pnl_fallback_for_track(track, pnls)
        if pnl is None:
            continue
        _refresh_peak_trail_from_upl(track, float(pnl))
        with _lock:
            live = _tracks.get(track.deal_id)
        if live is not None:
            _evaluate_track(live, float(pnl))
    with _lock:
        stale_ids = [
            k
            for k in list(_tracks.keys())
            if k not in open_ids and k not in _in_flight
        ]
    for stale_id in stale_ids:
        remove_track(stale_id)


def _pnl_fallback_for_track(
    track: GbpExitTrack, pnls: dict[str, float]
) -> float | None:
    """Match by epic when deal_id reconcile lags (single open on epic)."""
    epic = str(track.epic or "").strip()
    if not epic:
        return None
    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            positions = list(sync.snapshot().positions or [])
            for p in positions:
                if str(p.epic or "").strip() != epic:
                    continue
                if str(p.direction or "").upper() != str(track.direction or "").upper():
                    continue
                try:
                    if abs(float(p.size) - float(track.size)) > 0.02:
                        continue
                except (TypeError, ValueError):
                    continue
                deal_id = str(p.deal_id or "").strip()
                if deal_id in pnls:
                    return float(pnls[deal_id])
            same_epic = [p for p in positions if str(p.epic or "").strip() == epic]
            if len(same_epic) == 1:
                deal_id = str(same_epic[0].deal_id or "").strip()
                if deal_id in pnls:
                    return float(pnls[deal_id])
    except Exception:
        pass
    matches = [v for k, v in pnls.items() if k and v is not None]
    if len(matches) == 1 and len(_tracks) == 1:
        return float(matches[0])
    return None


def _progressive_lock_ratio(track: GbpExitTrack, peak: float, cfg: Any | None = None) -> float:
    from runtime.long_trade_runner import effective_lock_ratio

    return effective_lock_ratio(
        base_lock_ratio=float(track.trail_lock_ratio),
        peak=peak,
        trail_trigger_gbp=float(track.trail_trigger_gbp),
        armed_at=float(track.armed_at),
        peak_profit_gbp=float(track.peak_profit_gbp),
        cfg=cfg,
    )


def _update_trail_floor(track: GbpExitTrack, peak: float, cfg: Any | None = None) -> float:
    from runtime.long_trade_runner import effective_giveback_ratio

    lock = _progressive_lock_ratio(track, peak, cfg=cfg)
    locked = peak * lock
    giveback = effective_giveback_ratio(
        base_giveback=track.max_giveback_ratio,
        armed_at=track.armed_at,
        peak_profit_gbp=track.peak_profit_gbp,
        trail_trigger_gbp=track.trail_trigger_gbp,
        cfg=cfg,
    )
    giveback_floor = peak * (1.0 - giveback)
    return max(track.min_bank_win_gbp, locked, giveback_floor)


def _effective_target(track: GbpExitTrack, cfg: Any | None) -> float:
    from runtime.long_trade_runner import effective_target_gbp

    return effective_target_gbp(
        loss_cap_gbp=track.loss_cap_gbp,
        base_target_gbp=track.target_profit_gbp,
        armed_at=track.armed_at,
        peak_profit_gbp=track.peak_profit_gbp,
        trail_trigger_gbp=track.trail_trigger_gbp,
        cfg=cfg,
    )


def _refresh_peak_trail_from_upl(track: GbpExitTrack, pnl_gbp: float) -> None:
    """Sync peak/trail_floor from broker UPL on every watchdog tick."""
    if pnl_gbp <= track.peak_profit_gbp:
        return
    cfg = None
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        pass
    with _lock:
        live = _tracks.get(track.deal_id)
        if live is None:
            return
        live.peak_profit_gbp = max(live.peak_profit_gbp, pnl_gbp)
        if live.peak_profit_gbp >= live.trail_trigger_gbp:
            live.trail_floor_gbp = max(
                live.trail_floor_gbp,
                _update_trail_floor(live, live.peak_profit_gbp, cfg=cfg),
            )
            _publish_track_to_shm(live, pnl_gbp=pnl_gbp)


def _evaluate_track(track: GbpExitTrack, pnl_gbp: float) -> None:
    cfg = None
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        pass

    try:
        from execution.exit_execution_gate import is_paused

        if is_paused(track.deal_id):
            return
    except Exception:
        pass

    # EDITS_ONLY / instrument SUSPENDED — skip soft-loss / trail math entirely.
    # Keep a 10s async recovery poll armed; no sync flatten retries here.
    try:
        from execution.instrument_suspension import (
            bind_rest_client,
            is_deal_suspended,
            is_epic_suspended,
        )

        if is_deal_suspended(track.deal_id) or is_epic_suspended(track.epic):
            bind_rest_client(_resolve_rest_client())
            log_engine(
                f"MicroGbpExit: soft/trail SKIP instrument_SUSPENDED "
                f"deal={track.deal_id[:10]} epic={track.epic} pnl={pnl_gbp:.2f}"
            )
            return
    except Exception:
        pass

    # Peak bookkeeping always (even before hard-floor / soft cuts).
    if pnl_gbp > track.peak_profit_gbp:
        with _lock:
            live = _tracks.get(track.deal_id)
            if live is None:
                return
            live.peak_profit_gbp = pnl_gbp
            if pnl_gbp >= live.trail_trigger_gbp:
                live.trail_floor_gbp = max(
                    live.trail_floor_gbp,
                    _update_trail_floor(live, pnl_gbp, cfg=cfg),
                )
            _publish_track_to_shm(live, pnl_gbp=pnl_gbp)
            track = live

    # B1 air-gap: absolute UPL hard floor — does NOT require trusted entry.
    try:
        from execution.broker_upl_hard_floor import _floor_cfg

        floor = float(_floor_cfg(cfg)["floor_gbp"])
        if float(pnl_gbp) <= floor:
            _flatten(
                track,
                reason=f"broker_upl_hard_floor {pnl_gbp:.2f} <= {floor:.2f}",
                pnl_gbp=pnl_gbp,
            )
            return
    except Exception as exc:
        _flatten(
            track,
            reason=f"broker_upl_gate_exception:{type(exc).__name__}",
            pnl_gbp=pnl_gbp,
        )
        return

    if float(getattr(track, "entry_level", 0) or 0) <= 0:
        # Soft/trail skipped without entry; hard floor already evaluated above.
        log_engine(
            f"micro_gbp_exit soft/trail SKIP untrusted_entry deal={track.deal_id[:10]} "
            f"entry={getattr(track, 'entry_level', None)} pnl={pnl_gbp:.2f}"
        )
        return

    # 1) Soft loss — cut early before broker ~£20 stop on Gold min size.
    # Spread-widened chop can print soft_loss UPL without true adverse move —
    # widen effective soft toward attach ceiling until trail arms.
    effective_soft = float(track.soft_loss_gbp)
    try:
        from execution.open_position_rules import spread_adjusted_soft_loss_gbp

        effective_soft = spread_adjusted_soft_loss_gbp(
            epic=track.epic,
            size=track.size,
            soft_loss_gbp=track.soft_loss_gbp,
            loss_cap_gbp=track.loss_cap_gbp,
            trail_trigger_gbp=track.trail_trigger_gbp,
            peak_profit_gbp=track.peak_profit_gbp,
            cfg=cfg,
        )
    except Exception:
        effective_soft = float(track.soft_loss_gbp)

    if pnl_gbp <= -effective_soft:
        try:
            from execution.loss_patience import (
                loss_patience_enabled,
                should_hold_losing_position,
            )

            if loss_patience_enabled(cfg):
                decision = should_hold_losing_position(
                    epic=track.epic,
                    direction=track.direction,
                    pnl_gbp=pnl_gbp,
                    soft_loss_gbp=effective_soft,
                    loss_cap_gbp=track.loss_cap_gbp,
                    open_mins=(
                        max(0.0, (time.time() - float(track.armed_at or 0)) / 60.0)
                        if track.armed_at
                        else None
                    ),
                    cfg=cfg,
                )
                if decision.hold:
                    log_engine(
                        f"MicroGbpExit: loss_patience HOLD deal={track.deal_id[:10]} "
                        f"pnl={pnl_gbp:.2f} — {decision.reason}"
                    )
                    return
        except Exception:
            pass
        _flatten(
            track,
            reason=(
                f"soft_loss pnl={pnl_gbp:.2f} soft=-{effective_soft:.2f} "
                f"(base=-{track.soft_loss_gbp:.2f}) hard=-{track.loss_cap_gbp:.2f}"
            ),
            pnl_gbp=pnl_gbp,
        )
        return

    # 2) Hard loss cap.
    if pnl_gbp <= -track.loss_cap_gbp:
        _flatten(
            track,
            reason=f"loss_cap pnl={pnl_gbp:.2f} cap=-{track.loss_cap_gbp:.2f}",
            pnl_gbp=pnl_gbp,
        )
        return

    if _stagnant_flatten(track, pnl_gbp, cfg):
        return

    # 3) Track peak and ratchet trail floor.
    if pnl_gbp > track.peak_profit_gbp:
        with _lock:
            live = _tracks.get(track.deal_id)
            if live is None:
                return
            live.peak_profit_gbp = pnl_gbp
            if pnl_gbp >= live.trail_trigger_gbp:
                live.trail_floor_gbp = max(
                    live.trail_floor_gbp,
                    _update_trail_floor(live, pnl_gbp, cfg=cfg),
                )
            track = live

    from execution.tiered_profit_banks import tiered_bank_reason
    from runtime.long_trade_runner import is_long_runner_active, sb_prefer_long_hold

    long_runner = is_long_runner_active(
        armed_at=track.armed_at,
        peak_profit_gbp=track.peak_profit_gbp,
        trail_trigger_gbp=track.trail_trigger_gbp,
        cfg=cfg,
    )
    # SB / MACRO_SENTINEL: do not scratch with CFD scalp banks before runner age.
    sb_long = sb_prefer_long_hold(cfg)
    defer_scalp_banks = bool(long_runner or sb_long)

    # 4) Trailing profit exit — controlled giveback from peak.
    if track.peak_profit_gbp >= track.trail_trigger_gbp:
        floor = max(
            track.trail_floor_gbp,
            _update_trail_floor(track, track.peak_profit_gbp, cfg=cfg),
        )
        if pnl_gbp <= floor:
            trail_tag = "long_runner_profit_trail" if long_runner else "profit_trail"
            _flatten(
                track,
                reason=(
                    f"{trail_tag} pnl={pnl_gbp:.2f} floor={floor:.2f} "
                    f"peak={track.peak_profit_gbp:.2f}"
                ),
                pnl_gbp=pnl_gbp,
                exit_meta={"runner_extended": bool(long_runner)},
            )
            return

    pct_decision = None
    try:
        from execution.profit_pct_tiers import pct_tier_bank_reason, pct_tiers_enabled

        if pct_tiers_enabled(cfg):
            target = _effective_target(track, cfg)
            pct_decision = pct_tier_bank_reason(
                peak=track.peak_profit_gbp,
                pnl=pnl_gbp,
                target_gbp=target,
                epic=track.epic,
                direction=track.direction,
                trail_trigger_gbp=track.trail_trigger_gbp,
                armed_at=track.armed_at,
                cfg=cfg,
            )
            if pct_decision is not None:
                if pct_decision.runner_extended or defer_scalp_banks:
                    return
                _flatten(
                    track,
                    reason=pct_decision.reason,
                    pnl_gbp=pnl_gbp,
                    exit_meta={
                        "profit_tier_pct": pct_decision.tier_pct,
                        "peak_pct_of_target": pct_decision.peak_pct_of_target,
                        "profit_pct_of_target": pct_decision.profit_pct_of_target,
                        "runner_extended": False,
                    },
                )
                return
    except Exception:
        pass

    if not defer_scalp_banks:
        tier_reason = tiered_bank_reason(
            peak=track.peak_profit_gbp,
            pnl=pnl_gbp,
            trail_trigger_gbp=track.trail_trigger_gbp,
            cfg=cfg,
        )
        if tier_reason:
            _flatten(track, reason=tier_reason, pnl_gbp=pnl_gbp)
            return

    # 5) Quick win bank — optional; disabled when let_winners_run (trail handles exits).
    # SB long-prefer lane skips this so positions can age into long_trade_runner.
    quick_win_enabled = not sb_long
    try:
        from system.config_loader import get_config

        mr = get_config().get("micro_risk") or {}
        if isinstance(mr, dict) and mr.get("quick_win_bank_enabled") is False:
            quick_win_enabled = False
    except Exception:
        pass
    if (
        quick_win_enabled
        and not long_runner
        and track.peak_profit_gbp >= track.min_bank_win_gbp
        and track.peak_profit_gbp < track.trail_trigger_gbp
        and pnl_gbp <= track.peak_profit_gbp * 0.50
        and pnl_gbp >= track.min_bank_win_gbp * 0.80
    ):
        _flatten(
            track,
            reason=(
                f"quick_win pnl={pnl_gbp:.2f} peak={track.peak_profit_gbp:.2f} "
                f"min_bank={track.min_bank_win_gbp:.2f}"
            ),
            pnl_gbp=pnl_gbp,
        )
        return

    # 6) Hard target cap — extended when long-runner is active.
    target = _effective_target(track, cfg)
    if pnl_gbp >= target:
        tag = "long_runner_target_profit" if long_runner else "target_profit"
        _flatten(
            track,
            reason=f"{tag} pnl={pnl_gbp:.2f} tgt={target:.2f}",
            pnl_gbp=pnl_gbp,
            exit_meta={"runner_extended": bool(long_runner)},
        )


def _stagnant_flatten(track: GbpExitTrack, pnl_gbp: float, cfg: Any | None) -> bool:
    try:
        pm = {}
        if cfg is not None:
            pm = getattr(cfg, "position_management", None) or (
                cfg.get("position_management") if hasattr(cfg, "get") else {}
            )
        if not isinstance(pm, dict):
            return False
        stagnant_sec = float(pm.get("stagnant_flatten_sec") or 0)
        stagnant_band = float(pm.get("stagnant_pnl_band_gbp") or 1.0)
        if stagnant_sec <= 0:
            return False
        age = time.time() - float(track.armed_at or 0)
        if age < stagnant_sec:
            return False
        if -stagnant_band <= pnl_gbp <= stagnant_band:
            _flatten(
                track,
                reason=(
                    f"stagnant_dead_zone pnl={pnl_gbp:.2f} age={age:.0f}s "
                    f"band=±{stagnant_band:.2f}"
                ),
                pnl_gbp=pnl_gbp,
            )
            return True
    except Exception:
        pass
    return False


def _load_broker_pnls_gbp() -> dict[str, float]:
    """deal_id → unrealized P&L in GBP (sync cache, then REST with IG marks)."""
    out: dict[str, float] = {}
    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            for p in sync.snapshot().positions:
                from execution.position_pnl_gbp import pnl_gbp_for_open_row

                gbp = pnl_gbp_for_open_row(
                    epic=p.epic,
                    direction=p.direction,
                    entry_level=float(p.level),
                    size=float(p.size),
                    upl=float(p.upl) if abs(float(p.upl)) >= 0.001 else None,
                    bid=float(p.bid),
                    offer=float(p.offer),
                    currency=str(p.currency or ""),
                )
                if gbp is not None:
                    out[str(p.deal_id)] = float(gbp)
            if out:
                return out
    except Exception:
        pass

    try:
        from runtime import broker_snapshot

        shared = broker_snapshot.read_snapshot(max_age_sec=12.0)
        for p in shared.get("positions") or [] if shared else []:
            deal_id = str(p.get("deal_id") or "").strip()
            pnl = p.get("pnl_gbp")
            if deal_id and pnl is not None:
                out[deal_id] = float(pnl)
        if out:
            return out
    except Exception:
        pass

    rest = _rest_client
    if rest is None or not hasattr(rest, "open_positions"):
        return out
    try:
        from execution.position_pnl_gbp import pnl_gbp_from_ig_item

        for item in rest.open_positions(budget_priority=True) or []:
            pos = item.get("position") or {}
            deal_id = str(pos.get("dealId") or pos.get("dealID") or "").strip()
            if not deal_id:
                continue
            gbp = pnl_gbp_from_ig_item(item)
            if gbp is not None:
                out[deal_id] = float(gbp)
        return out
    except Exception as exc:
        log_engine(f"MicroGbpExit: UPL poll failed: {type(exc).__name__}: {exc}")
        return out


def _flatten(
    track: GbpExitTrack,
    *,
    reason: str,
    pnl_gbp: float | None = None,
    exit_meta: dict[str, Any] | None = None,
) -> None:
    """Route all micro flattens through the single exit execution gate (sync)."""
    try:
        from execution.exit_execution_gate import is_executing, request_flatten

        if is_executing(track.deal_id):
            return
        rest = _resolve_rest_client()
        request_flatten(
            rest=rest,
            deal_id=track.deal_id,
            epic=track.epic,
            direction=track.direction,
            size=track.size,
            reason=reason,
            pnl_gbp=pnl_gbp,
            source="micro_gbp_exit",
        )
        return
    except Exception as exc:
        log_engine(f"MicroGbpExit: exit_gate failed — {type(exc).__name__}: {exc}")

    # Legacy fallback if gate import fails.
    with _lock:
        if track.deal_id in _in_flight:
            return
        _in_flight.add(track.deal_id)
    log_engine(
        f"MicroGbpExit: flatten deal={track.deal_id} epic={track.epic} — {reason}"
    )
    threading.Thread(
        target=_flatten_sync,
        args=(track, reason, pnl_gbp, exit_meta or {}),
        name=f"gbp-exit-{track.deal_id[:10]}",
        daemon=True,
    ).start()


def _resolve_rest_client() -> Any | None:
    rest = _rest_client
    if rest is not None:
        return rest
    try:
        from runtime.trade_manager import get_dual_core_coordinator

        coord = get_dual_core_coordinator()
        return getattr(coord, "_rest", None) if coord else None
    except Exception:
        return None


def _flatten_sync(
    track: GbpExitTrack,
    reason: str = "",
    pnl_gbp: float | None = None,
    exit_meta: dict[str, Any] | None = None,
) -> None:
    rest = _resolve_rest_client()
    meta = dict(exit_meta or {})
    try:
        if rest is None:
            return
        # close_position(skip_lookup=True) inverts OPEN once — pass OPEN side.
        open_side = str(track.direction or "BUY").upper()
        if hasattr(rest, "close_position"):
            rest.close_position(
                track.deal_id,
                direction=open_side,
                size=track.size,
                epic=track.epic,
                verify=False,
                budget_priority=True,
                skip_lookup=True,
                skip_confirm=True,
            )
        elif hasattr(rest, "flatten_epic_positions"):
            rest.flatten_epic_positions(track.epic)
    except Exception as exc:
        log_engine(
            f"MicroGbpExit: flatten failed deal={track.deal_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            from execution.instrument_suspension import (
                bind_rest_client,
                is_instrument_restriction,
                mark_deal_suspended,
            )
            from ig_api.exceptions import InstrumentSuspendedException

            if isinstance(exc, InstrumentSuspendedException) or is_instrument_restriction(
                exc
            ):
                bind_rest_client(rest)
                mark_deal_suspended(
                    track.deal_id,
                    epic=track.epic,
                    status=getattr(exc, "status", None) or "EDITS_ONLY",
                    detail=str(exc),
                    entry_level=float(track.entry_level or 0),
                    direction=track.direction,
                    size=float(track.size or 0),
                )
                try:
                    from execution.edits_only_close_queue import enqueue_close

                    enqueue_close(
                        deal_id=track.deal_id,
                        epic=track.epic,
                        direction=track.direction,
                        size=float(track.size or 0),
                        reason=reason or "micro_gbp_edits_only",
                        error=str(exc),
                        pnl_gbp=pnl_gbp,
                    )
                except Exception:
                    pass
        except Exception:
            pass
    else:
        session_slot = None
        try:
            from execution.profit_pct_tiers import (
                capture_exit_context,
                classify_tier_pct_from_reason,
            )
            from runtime.strategy_improvement_tracker import record_managed_close

            ctx = capture_exit_context(epic=track.epic, direction=track.direction)
            hold_sec = max(0.0, time.time() - float(track.armed_at or 0))
            tier_pct = meta.get("profit_tier_pct")
            if tier_pct is None:
                tier_pct = classify_tier_pct_from_reason(reason)
            session_slot = record_managed_close(
                epic=track.epic,
                pnl_gbp=float(pnl_gbp if pnl_gbp is not None else 0.0),
                exit_reason=reason or "micro_gbp_exit",
                profit_tier_pct=float(tier_pct) if tier_pct is not None else None,
                peak_pct_of_target=meta.get("peak_pct_of_target"),
                profit_pct_of_target=meta.get("profit_pct_of_target"),
                hold_sec=hold_sec,
                sentiment_delta_5m=ctx.get("sentiment_delta_5m"),
                news_countdown_norm=ctx.get("news_countdown_norm"),
                runner_extended=bool(meta.get("runner_extended", False)),
            )
        except Exception:
            session_slot = None
        try:
            from execution.ml_training_hooks import record_ml_exit_for_deal

            gbp = float(pnl_gbp if pnl_gbp is not None else 0.0)
            result = "WIN" if gbp > 0.05 else ("LOSS" if gbp < -0.05 else "BREAKEVEN")
            record_ml_exit_for_deal(
                track.deal_id,
                ig_pnl=gbp,
                result=result,
                exit_reason=reason or "micro_gbp_exit",
                profit_tier_pct=meta.get("profit_tier_pct"),
                peak_pct_of_target=meta.get("peak_pct_of_target"),
                profit_pct_of_target=meta.get("profit_pct_of_target"),
                hold_sec=max(0.0, time.time() - float(track.armed_at or 0)),
                session_slot=session_slot,
            )
        except Exception:
            pass
    finally:
        remove_track(track.deal_id)
        try:
            from runtime.dynamic_limit_engine import remove_track as remove_dyn
            from runtime.virtual_stop_loss import clear_virtual_stop

            clear_virtual_stop(track.deal_id)
            remove_dyn(track.deal_id)
        except Exception:
            pass
        with _lock:
            _in_flight.discard(track.deal_id)


def hydrate_open_positions_from_broker(rest_client: Any | None, cfg: Any | None = None) -> int:
    """Arm full risk stack for all open IG positions (boot + manual reconcile)."""
    from execution.position_risk_stack import reconcile_open_positions_risk_stack

    counts = reconcile_open_positions_risk_stack(rest_client, cfg=cfg, force=True)
    return int(counts.get("armed", 0)) + int(counts.get("gbp", 0))
