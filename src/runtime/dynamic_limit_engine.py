"""Dynamic profit trail — ratchets limits on hub mids; optional IG broker PUT sync.

Demo soak default: software-only trail (no broker PUT). Broker edits are gated by
config, REST pressure (ELEVATED+), and a fail circuit-breaker so 403/404 spam
cannot starve GET /positions.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.pnl_math import ig_points_to_price_delta, price_delta_to_ig_points

_lock = threading.RLock()
_tracks: dict[str, "DynamicLimitTrack"] = {}
_active = False
_rest_client: Any | None = None
_in_flight: set[str] = set()

# Successful broker trail spacing (was 2.5s — too hot under multi-deal books).
_BROKER_TRAIL_MIN_SEC = 45.0
# After any PUT failure, cool that deal before retry.
_BROKER_PUT_FAIL_COOLDOWN_SEC = 180.0
# Global circuit after repeated hard failures (403/404/allowance).
_BROKER_PUT_CIRCUIT_FAILS = 2
_BROKER_PUT_CIRCUIT_SEC = 600.0
_REST_BLOCK_LEVELS = frozenset({"ELEVATED", "HIGH", "CRITICAL"})

_broker_put_circuit_until = 0.0
_broker_put_fail_streak = 0
_last_software_only_log_at = 0.0

# Software flatten circuit — stop DELETE/net-close spam after repeated failures.
_FLATTEN_FAIL_MAX = 3
_FLATTEN_COOLDOWN_SEC = 300.0
_flatten_fail_counts: dict[str, int] = {}
_flatten_circuit_until: dict[str, float] = {}


@dataclass
class DynamicLimitTrack:
    deal_id: str
    epic: str
    direction: str
    entry_level: float
    size: float
    limit_level: float
    limit_pts: float
    peak_profit_ig_pts: float = 0.0
    trail_trigger_ig_pts: float = 1.5
    trail_lock_ratio: float = 0.70
    updated_at: float = 0.0
    armed_at: float = 0.0
    last_broker_trail_at: float = 0.0
    broker_trail_iterations: int = 0
    broker_put_disabled: bool = False
    broker_put_fail_count: int = 0
    last_broker_put_fail_at: float = 0.0


_FANTASY_PEAK_JUMP_PTS = 25.0
_FANTASY_INDEX_PEAK_CAP_PTS = 80.0


def _min_hold_before_trail_sec(cfg: Any | None = None) -> float:
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        if cfg is not None and hasattr(cfg, "get"):
            mr = cfg.get("micro_risk") or {}
            if isinstance(mr, dict) and mr.get("min_hold_before_trail_sec") is not None:
                return max(0.0, float(mr.get("min_hold_before_trail_sec")))
    except Exception:
        pass
    return 150.0


def _sb_skip_dynamic_trail(track: "DynamicLimitTrack", cfg: Any | None = None) -> bool:
    """SB long-runner lane: no DynamicLimit trail flatten until runner armed."""
    try:
        from runtime.long_trade_runner import (
            is_long_runner_active,
            sb_prefer_long_hold,
            skip_dynamic_limit_until_armed,
        )

        if not skip_dynamic_limit_until_armed(cfg):
            return False
        if not sb_prefer_long_hold(cfg):
            return False
        peak_gbp = float(track.peak_profit_ig_pts) * max(0.01, float(track.size))
        # Approximate GBP peak from pts×size; trail_trigger from micro_risk.
        trail_trig = 2.5
        try:
            if cfg is None:
                from system.config_loader import get_config

                cfg = get_config()
            mr = (cfg.get("micro_risk") or {}) if cfg is not None and hasattr(cfg, "get") else {}
            if isinstance(mr, dict) and mr.get("trail_trigger_gbp") is not None:
                trail_trig = float(mr.get("trail_trigger_gbp"))
        except Exception:
            pass
        if is_long_runner_active(
            armed_at=float(track.armed_at or 0.0),
            peak_profit_gbp=peak_gbp,
            trail_trigger_gbp=trail_trig,
            cfg=cfg,
        ):
            return False
        return True
    except Exception:
        return False


def start_dynamic_limit_engine() -> None:
    global _active
    with _lock:
        _active = True
    try:
        from system.unified_runtime_state import update_stops_limits

        update_stops_limits(dynamic_limit_active=True)
    except Exception:
        pass
    mode = "software-only" if not _broker_puts_allowed() else "broker-sync"
    log_engine(f"DynamicLimit: engine active (profit trail={mode})")


def bind_rest_client(rest_client: Any | None) -> None:
    global _rest_client
    _rest_client = rest_client


def _software_only_killfile_active() -> bool:
    try:
        from system.paths import data_dir

        path = Path(data_dir()) / "state" / "dynamic_limit_software_only.json"
        if not path.is_file():
            return False
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        return bool(raw.get("active", True))
    except Exception:
        return False


def _omit_broker_trail_configured() -> bool:
    env = os.environ.get("IG_DYNAMIC_LIMIT_SOFTWARE_ONLY", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        from execution.micro_risk_profile import omit_broker_trail_updates

        return bool(omit_broker_trail_updates())
    except Exception:
        return True  # fail-closed: software trail only


def _rest_pressure_blocks_put() -> bool:
    try:
        from system.rest_api_budget import get_rest_api_budget

        level = str(get_rest_api_budget().pressure_level() or "IDLE").upper()
        return level in _REST_BLOCK_LEVELS
    except Exception:
        return False


def _broker_puts_allowed() -> bool:
    """False → ratchet + flatten in software only (no broker PUT)."""
    if _omit_broker_trail_configured():
        return False
    if _software_only_killfile_active():
        return False
    if _rest_pressure_blocks_put():
        return False
    with _lock:
        if time.time() < _broker_put_circuit_until:
            return False
    return True


def _log_software_only_once(reason: str) -> None:
    global _last_software_only_log_at
    now = time.time()
    if now - _last_software_only_log_at < 30.0:
        return
    _last_software_only_log_at = now
    log_engine(f"DynamicLimit: broker PUT suppressed ({reason}) — software trail only")


def _trip_broker_put_circuit(reason: str, *, deal_id: str | None = None) -> None:
    global _broker_put_circuit_until, _broker_put_fail_streak
    with _lock:
        _broker_put_fail_streak += 1
        streak = _broker_put_fail_streak
        if deal_id:
            track = _tracks.get(str(deal_id))
            if track is not None:
                track.broker_put_disabled = True
                track.last_broker_put_fail_at = time.time()
                track.last_broker_trail_at = time.time()
        if streak >= _BROKER_PUT_CIRCUIT_FAILS:
            _broker_put_circuit_until = time.time() + _BROKER_PUT_CIRCUIT_SEC
            until = _broker_put_circuit_until
        else:
            until = 0.0
    if until > 0:
        log_engine(
            f"DynamicLimit: broker PUT circuit OPEN for {_BROKER_PUT_CIRCUIT_SEC:.0f}s "
            f"({reason} streak={streak})"
        )
    else:
        log_engine(f"DynamicLimit: broker PUT fail cooldown ({reason} streak={streak})")


def _favorable_ig_pts(*, epic: str, direction: str, entry_level: float, mid: float) -> float:
    if entry_level <= 0 or mid <= 0:
        return 0.0
    d = str(direction or "").upper()
    raw = (mid - entry_level) if d == "BUY" else (entry_level - mid)
    return max(0.0, price_delta_to_ig_points(epic, raw))


def _limit_level_from_pts(
    *,
    epic: str,
    direction: str,
    entry_level: float,
    limit_pts: float,
) -> float:
    delta = ig_points_to_price_delta(epic, max(0.0, float(limit_pts)))
    d = str(direction or "").upper()
    return entry_level + delta if d == "BUY" else entry_level - delta


def register_dynamic_limit(
    *,
    deal_id: str,
    epic: str,
    direction: str,
    entry_level: float,
    limit_pts: float,
    size: float = 1.0,
    trail_trigger_ig_pts: float | None = None,
    trail_lock_ratio: float | None = None,
) -> None:
    """Arm dynamic profit trail for a trade."""
    key = str(deal_id or epic)
    d = str(direction or "BUY").upper()
    pts = max(0.5, float(limit_pts))
    limit_level = _limit_level_from_pts(
        epic=str(epic),
        direction=d,
        entry_level=float(entry_level),
        limit_pts=pts,
    )
    trigger = float(trail_trigger_ig_pts) if trail_trigger_ig_pts is not None else max(1.0, pts * 0.35)
    lock = float(trail_lock_ratio) if trail_lock_ratio is not None else 0.70
    with _lock:
        _tracks[key] = DynamicLimitTrack(
            deal_id=key,
            epic=str(epic),
            direction=d,
            entry_level=float(entry_level),
            size=max(0.01, float(size)),
            limit_level=float(limit_level),
            limit_pts=pts,
            peak_profit_ig_pts=0.0,
            trail_trigger_ig_pts=trigger,
            trail_lock_ratio=max(0.4, min(0.95, lock)),
            updated_at=time.time(),
            armed_at=time.time(),
        )
    _publish(key)
    try:
        from runtime.trade_lifecycle import LifecycleState, transition

        transition(key, LifecycleState.DYNAMIC_LIMIT_ACTIVE, message="Dynamic profit trail armed")
    except Exception:
        pass


def update_from_mid(epic: str, mid: float) -> None:
    """Ratchet profit floor as price moves favorably (never lower the trail)."""
    if mid <= 0:
        return
    key = str(epic or "").strip()
    with _lock:
        for track_id, track in list(_tracks.items()):
            if track.epic != key:
                continue
            profit_ig = _favorable_ig_pts(
                epic=track.epic,
                direction=track.direction,
                entry_level=track.entry_level,
                mid=float(mid),
            )
            if profit_ig <= track.peak_profit_ig_pts:
                continue
            # Reject fantasy peaks (Yahoo/hub scale noise) — CFD and SB alike.
            # • First print from flat >12pt on index = bogus (forensic 19.5pt peak)
            # • Any single jump >25pt rejected
            # • Absolute peak >80pt on index rejected
            jump = profit_ig - float(track.peak_profit_ig_pts or 0.0)
            prev_peak = float(track.peak_profit_ig_pts or 0.0)
            indexish = float(track.entry_level or 0.0) > 1000.0
            if indexish and prev_peak < 1.0 and profit_ig > 12.0:
                log_engine(
                    f"DynamicLimit: fantasy first-peak rejected epic={track.epic} "
                    f"profit={profit_ig:.1f}pt (cap=12 first print)"
                )
                continue
            if jump > _FANTASY_PEAK_JUMP_PTS:
                log_engine(
                    f"DynamicLimit: fantasy peak rejected epic={track.epic} "
                    f"jump={jump:.1f}pt profit={profit_ig:.1f} prev={prev_peak:.1f}"
                )
                continue
            if profit_ig > _FANTASY_INDEX_PEAK_CAP_PTS and indexish:
                continue
            track.peak_profit_ig_pts = profit_ig
            if profit_ig < track.trail_trigger_ig_pts:
                continue
            # Lock a fraction of peak profit — progressive trail follows winners.
            lock_ratio = min(0.92, track.trail_lock_ratio + min(0.12, profit_ig * 0.008))
            trail_pts = max(track.limit_pts, profit_ig * lock_ratio)
            if trail_pts <= track.limit_pts + 1e-9:
                continue
            track.limit_pts = trail_pts
            track.limit_level = _limit_level_from_pts(
                epic=track.epic,
                direction=track.direction,
                entry_level=track.entry_level,
                limit_pts=trail_pts,
            )
            track.updated_at = time.time()
            _publish(track_id)


def check_limit_hit(epic: str, mid: float) -> list[str]:
    """Return deal_ids whose profit target or ratcheted trail was touched."""
    hits: list[str] = []
    if mid <= 0:
        return hits
    key = str(epic or "").strip()
    min_hold = _min_hold_before_trail_sec()
    now = time.time()
    with _lock:
        for track_id, track in list(_tracks.items()):
            if track.epic != key:
                continue
            # Min-hold: block trail/target flattens until aged (hard VSL separate).
            age = now - float(track.armed_at or 0.0)
            if min_hold > 0 and age < min_hold:
                continue
            if _sb_skip_dynamic_trail(track):
                continue
            # Profit-run: skip hyper-sensitive DynamicLimit trail flattens; hard VSL stays.
            try:
                from runtime.profit_run_policy import should_skip_dynamic_limit_hyper

                approx_gbp = float(track.peak_profit_ig_pts or 0.0) * float(
                    track.size or 0.5
                )
                if should_skip_dynamic_limit_hyper(
                    unrealized_pnl_gbp=approx_gbp, cfg=None
                ):
                    continue
            except Exception:
                pass
            d = track.direction
            trailing = track.peak_profit_ig_pts >= track.trail_trigger_ig_pts
            if d == "BUY":
                if trailing:
                    if mid <= track.limit_level:
                        hits.append(track_id)
                elif mid >= track.limit_level:
                    hits.append(track_id)
            elif trailing:
                if mid >= track.limit_level:
                    hits.append(track_id)
            elif mid <= track.limit_level:
                hits.append(track_id)
    return hits


def _broker_mark_for_track(track: DynamicLimitTrack) -> float | None:
    """IG bid/offer mark for dynamic trail — avoids Yahoo hub scale mismatch."""
    from trading.open_position_view import _quote_mark_trustworthy

    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            for p in sync.snapshot().positions:
                if str(p.deal_id) != str(track.deal_id):
                    continue
                mark = float(p.bid if track.direction == "BUY" else p.offer)
                if mark > 0 and _quote_mark_trustworthy(track.entry_level, mark, track.epic):
                    return mark
    except Exception:
        pass
    from system.market_data_hub import get_market_data_hub

    snap = get_market_data_hub().get_snapshot(track.epic)
    if snap is None or snap.bid <= 0 or snap.offer <= 0:
        return None
    mid = (float(snap.bid) + float(snap.offer)) / 2.0
    from trading.open_position_view import mark_within_ig_basis

    if mark_within_ig_basis(track.entry_level, mid, track.epic, max_ig_pts=25.0):
        return mid
    return None


def on_watchdog_tick() -> None:
    """Evaluate profit trail from IG broker marks (~500ms via virtual-stop watchdog)."""
    if not _active:
        return
    with _lock:
        tracks = list(_tracks.values())
    for track in tracks:
        if track.deal_id in _in_flight:
            continue
        mark = _broker_mark_for_track(track)
        if mark is None or mark <= 0:
            continue
        update_from_mid(track.epic, mark)
        for deal_id in check_limit_hit(track.epic, mark):
            _take_profit_flatten(deal_id)
        _maybe_broker_trail(track.epic, mark)


def on_streaming_mid_tick(epic: str, mid: float) -> None:
    """Hub hook — ratchet profit trail when mark scale matches IG entry."""
    if mid <= 0:
        return
    from trading.open_position_view import mark_within_ig_basis

    with _lock:
        tracks = [t for t in _tracks.values() if t.epic == str(epic or "").strip()]
    # Yahoo–IG basis of ~60pt must not false-hit initial TP (peak still 0).
    if tracks and not any(
        mark_within_ig_basis(t.entry_level, mid, epic, max_ig_pts=25.0) for t in tracks
    ):
        return
    update_from_mid(epic, mid)
    for deal_id in check_limit_hit(epic, mid):
        _take_profit_flatten(deal_id)
    _maybe_broker_trail(epic, mid)


def _maybe_broker_trail(epic: str, mid: float) -> None:
    global _broker_put_fail_streak
    if not _broker_puts_allowed():
        if _omit_broker_trail_configured() or _software_only_killfile_active():
            _log_software_only_once("config/killfile")
        elif _rest_pressure_blocks_put():
            _log_software_only_once("REST ELEVATED+")
        else:
            _log_software_only_once("circuit_open")
        return
    rest = _rest_client
    if rest is None:
        return
    now = time.time()
    key = str(epic or "").strip()
    with _lock:
        candidates = [
            t
            for t in _tracks.values()
            if t.epic == key
            and t.peak_profit_ig_pts >= t.trail_trigger_ig_pts
            and not t.broker_put_disabled
        ]
    for track in candidates:
        if now - track.last_broker_trail_at < _BROKER_TRAIL_MIN_SEC:
            continue
        if track.last_broker_put_fail_at and (
            now - track.last_broker_put_fail_at < _BROKER_PUT_FAIL_COOLDOWN_SEC
        ):
            continue
        try:
            from execution.live_broker_order_router import (
                apply_step_trail_put,
                compute_step_trail_update,
            )

            step = max(1.0, track.trail_trigger_ig_pts * 0.5)
            scalp_limit = max(track.limit_pts, track.peak_profit_ig_pts * 0.9)
            update = compute_step_trail_update(
                rest,
                epic=track.epic,
                direction=track.direction,
                deal_id=track.deal_id,
                entry_level=track.entry_level,
                step_points=step,
                scalp_limit_points=scalp_limit,
                iteration=track.broker_trail_iterations,
                market_price=float(mid),
            )
            # Never bypass REST budget — trail PUTs must not starve GET /positions.
            apply_step_trail_put(rest, update, budget_priority=False)
            with _lock:
                live = _tracks.get(track.deal_id)
                if live is not None:
                    live.last_broker_trail_at = now
                    live.broker_trail_iterations += 1
                    live.broker_put_fail_count = 0
                _broker_put_fail_streak = 0
        except Exception as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            msg = str(exc)
            hard = status in (403, 404) or "exceeded-account-trading-allowance" in msg
            with _lock:
                live = _tracks.get(track.deal_id)
                if live is not None:
                    live.last_broker_trail_at = now
                    live.last_broker_put_fail_at = now
                    live.broker_put_fail_count += 1
                    # 404 / null position details → stop PUTs for this deal forever.
                    if status == 404 or "position.details.null" in msg:
                        live.broker_put_disabled = True
            log_engine(
                f"DynamicLimit: broker trail failed epic={track.epic} deal={track.deal_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            if hard:
                _trip_broker_put_circuit(f"HTTP {status or 'err'}", deal_id=track.deal_id)


def _flatten_circuit_open(deal_id: str) -> bool:
    did = str(deal_id or "").strip()
    if not did:
        return False
    with _lock:
        until = float(_flatten_circuit_until.get(did) or 0.0)
    if until <= 0:
        return False
    if time.time() < until:
        return True
    with _lock:
        _flatten_circuit_until.pop(did, None)
        _flatten_fail_counts.pop(did, None)
    return False


def _record_flatten_failure(deal_id: str, *, detail: str = "") -> None:
    did = str(deal_id or "").strip()
    if not did:
        return
    with _lock:
        n = int(_flatten_fail_counts.get(did) or 0) + 1
        _flatten_fail_counts[did] = n
        if n >= _FLATTEN_FAIL_MAX:
            _flatten_circuit_until[did] = time.time() + _FLATTEN_COOLDOWN_SEC
            log_engine(
                f"DynamicLimit: flatten circuit OPEN deal={did[:12]} fails={n} "
                f"cooldown={_FLATTEN_COOLDOWN_SEC:.0f}s {detail}"
            )


def _clear_flatten_failure(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _flatten_fail_counts.pop(did, None)
        _flatten_circuit_until.pop(did, None)


def _take_profit_flatten(deal_id: str) -> None:
    with _lock:
        if deal_id in _in_flight:
            return
        track = _tracks.get(deal_id)
        if track is None:
            return
        if _flatten_circuit_open(deal_id):
            return
        min_hold = _min_hold_before_trail_sec()
        age = time.time() - float(track.armed_at or 0.0)
        if min_hold > 0 and age < min_hold:
            return
        if _sb_skip_dynamic_trail(track):
            return
        _in_flight.add(deal_id)
    log_engine(
        f"DynamicLimit: profit trail hit — flatten deal={deal_id} epic={track.epic} "
        f"peak={track.peak_profit_ig_pts:.2f}pt"
    )
    threading.Thread(
        target=_flatten_sync,
        args=(track,),
        name=f"dyn-limit-flat-{deal_id[:12]}",
        daemon=True,
    ).start()


def _flatten_sync(track: DynamicLimitTrack) -> None:
    """Software flatten via exit gate / OPEN-side close_position(skip_lookup=True)."""
    rest = _rest_client
    deal_id = track.deal_id
    ok = False
    try:
        if rest is None:
            return
        # Prefer the single exit gate (OPEN side + confirm).
        try:
            from execution.exit_execution_gate import is_executing, request_flatten

            if is_executing(deal_id):
                return
            result = request_flatten(
                rest=rest,
                deal_id=deal_id,
                epic=track.epic,
                direction=track.direction,  # OPEN side
                size=track.size,
                reason="dynamic_limit_trail",
                source="dynamic_limit",
            )
            ok = bool(result.get("ok") or result.get("already_flat"))
            if not ok and not result.get("skipped"):
                _record_flatten_failure(
                    deal_id, detail=str(result.get("error") or "not_ok")
                )
            elif ok:
                _clear_flatten_failure(deal_id)
            return
        except Exception as gate_exc:
            log_engine(
                f"DynamicLimit: exit_gate failed — {type(gate_exc).__name__}: {gate_exc}"
            )

        # Legacy fallback: close_position(skip_lookup=True) inverts OPEN once.
        # Pass OPEN side — never pre-inverted close_dir.
        open_side = str(track.direction or "BUY").upper()
        if deal_id and hasattr(rest, "close_position"):
            rest.close_position(
                deal_id,
                direction=open_side,
                size=track.size,
                epic=track.epic,
                verify=False,
                budget_priority=True,
                skip_lookup=True,
                skip_confirm=True,
            )
            ok = True
            _clear_flatten_failure(deal_id)
        elif hasattr(rest, "flatten_epic_positions"):
            rest.flatten_epic_positions(track.epic)
            ok = True
            _clear_flatten_failure(deal_id)
    except Exception as exc:
        _record_flatten_failure(deal_id, detail=f"{type(exc).__name__}: {exc}")
        log_engine(
            f"DynamicLimit: flatten failed epic={track.epic}: {type(exc).__name__}: {exc}"
        )
    finally:
        # Keep track on failure so trail can cool; drop only after success or circuit.
        if ok or _flatten_circuit_open(deal_id):
            remove_track(deal_id)
        with _lock:
            _in_flight.discard(deal_id)


def remove_track(deal_id: str) -> None:
    with _lock:
        _tracks.pop(str(deal_id), None)


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "active": _active,
            "broker_puts_allowed": _broker_puts_allowed(),
            "broker_put_circuit_until": _broker_put_circuit_until,
            "tracks": {
                k: {
                    "epic": v.epic,
                    "direction": v.direction,
                    "entry_level": v.entry_level,
                    "limit_level": v.limit_level,
                    "limit_pts": v.limit_pts,
                    "peak_profit_ig_pts": v.peak_profit_ig_pts,
                    "updated_at": v.updated_at,
                    "broker_put_disabled": v.broker_put_disabled,
                }
                for k, v in _tracks.items()
            },
        }


def reset_dynamic_limit_for_tests() -> None:
    global _active, _rest_client, _broker_put_circuit_until, _broker_put_fail_streak
    global _last_software_only_log_at
    with _lock:
        _tracks.clear()
        _in_flight.clear()
        _flatten_fail_counts.clear()
        _flatten_circuit_until.clear()
        _active = False
        _broker_put_circuit_until = 0.0
        _broker_put_fail_streak = 0
        _last_software_only_log_at = 0.0
    _rest_client = None


def _publish(deal_id: str) -> None:
    with _lock:
        track = _tracks.get(deal_id)
        if track is None:
            return
        row = {
            "epic": track.epic,
            "dynamic_limit_level": track.limit_level,
            "dynamic_limit_pts": track.limit_pts,
            "peak_profit_ig_pts": track.peak_profit_ig_pts,
        }
    try:
        from system.unified_runtime_state import update_stops_limits

        update_stops_limits(
            dynamic_limit_active=True,
            deal_id=deal_id,
            trade_state=row,
        )
    except Exception:
        pass
