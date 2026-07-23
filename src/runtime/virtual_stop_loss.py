"""
Internal virtual stop-loss — 2.0pt capital ceiling with 500ms tick watchdog.

Broker-facing stops are stretched to max(2.0, minStopOrProfitDistance).
When streaming mid moves 2.0 IG points against the position, dispatch an
un-throttled async market flatten on the uvicorn boot loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any

from system.engine_log import log_engine
from system.pnl_math import ig_points_to_price_delta

INTERNAL_RISK_CEILING_PTS = 2.0
internal_risk_ceiling = INTERNAL_RISK_CEILING_PTS  # local virtual watchdog ceiling (pts)
VIRTUAL_STOP_WATCHDOG_SEC = 0.5

_lock = threading.Lock()
_stop = threading.Event()
_watchdog_thread: threading.Thread | None = None
_rest_client: Any | None = None
_last_coverage_reconcile_at = 0.0
_positions: dict[str, "VirtualStopTrack"] = {}
_in_flight: set[str] = set()

# After N failed flattens per deal, cool down so DELETE/net-close cannot spam REST.
_FLATTEN_FAIL_MAX = 3
_FLATTEN_COOLDOWN_SEC = 300.0
_flatten_fail_counts: dict[str, int] = {}
_flatten_circuit_until: dict[str, float] = {}


@dataclass
class VirtualStopTrack:
    track_id: str
    deal_id: str
    epic: str
    direction: str
    entry_level: float
    size: float
    armed_at: float
    ceiling_pts: float = INTERNAL_RISK_CEILING_PTS


def stretch_broker_stop_distance(
    rest_client: Any | None,
    epic: str,
    requested_points: float,
) -> float:
    """Stop_Loss = max(2.0, minStopOrProfitDistance, requested)."""
    from execution.live_broker_order_router import resolve_min_stop_distance_points

    broker_min = resolve_min_stop_distance_points(rest_client, epic)
    return max(INTERNAL_RISK_CEILING_PTS, float(broker_min), float(requested_points))


def _flatten_circuit_open_unlocked(deal_id: str) -> bool:
    did = str(deal_id or "").strip()
    if not did:
        return False
    until = float(_flatten_circuit_until.get(did) or 0.0)
    if until <= 0:
        return False
    if time.time() < until:
        return True
    _flatten_circuit_until.pop(did, None)
    _flatten_fail_counts.pop(did, None)
    return False


def flatten_circuit_open(deal_id: str) -> bool:
    with _lock:
        return _flatten_circuit_open_unlocked(deal_id)


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
                f"VirtualStop: flatten circuit OPEN deal={did[:12]} fails={n} "
                f"cooldown={_FLATTEN_COOLDOWN_SEC:.0f}s {detail}"
            )


def _clear_flatten_failure(deal_id: str) -> None:
    did = str(deal_id or "").strip()
    with _lock:
        _flatten_fail_counts.pop(did, None)
        _flatten_circuit_until.pop(did, None)


def register_virtual_stop(
    *,
    epic: str,
    direction: str,
    entry_level: float,
    size: float,
    deal_id: str = "",
    ceiling_pts: float | None = None,
) -> str:
    """Arm internal virtual ceiling for an open contract."""
    ceiling = float(ceiling_pts) if ceiling_pts is not None else INTERNAL_RISK_CEILING_PTS
    track_id = deal_id or f"{epic}:{direction}:{int(time.time() * 1000)}"
    with _lock:
        # Still arm for GUI/coverage, but do not evaluate flatten while circuit is open.
        _positions[track_id] = VirtualStopTrack(
            track_id=track_id,
            deal_id=str(deal_id or ""),
            epic=str(epic),
            direction=str(direction or "BUY").upper(),
            entry_level=float(entry_level),
            size=float(size),
            armed_at=time.time(),
            ceiling_pts=ceiling,
        )
    log_engine(
        f"VirtualStop: armed track={track_id} epic={epic} {direction} "
        f"entry={entry_level} ceiling={ceiling}pt"
    )
    try:
        from system.unified_runtime_state import update_stops_limits

        update_stops_limits(
            trailing_active=True,
            dynamic_limit_active=True,
            deal_id=str(deal_id or track_id),
            trade_state={
                "epic": epic,
                "direction": direction,
                "entry_level": entry_level,
                "ceiling_pts": ceiling,
            },
        )
    except Exception:
        pass
    return track_id


def virtual_stop_snapshot() -> dict[str, Any]:
    """Export armed virtual stops for unified status / GUI."""
    with _lock:
        return {
            "count": len(_positions),
            "watchdog_active": _watchdog_thread is not None and _watchdog_thread.is_alive(),
            "positions": [
                {
                    "track_id": t.track_id,
                    "deal_id": t.deal_id,
                    "epic": t.epic,
                    "direction": t.direction,
                    "entry_level": t.entry_level,
                    "size": t.size,
                    "ceiling_pts": t.ceiling_pts,
                    "armed_at": t.armed_at,
                }
                for t in _positions.values()
            ],
        }


def clear_virtual_stop(track_id: str) -> None:
    with _lock:
        _positions.pop(track_id, None)
        _in_flight.discard(track_id)


def reset_virtual_stop_for_tests() -> None:
    global _watchdog_thread, _rest_client, _last_coverage_reconcile_at
    _stop.set()
    if _watchdog_thread is not None:
        _watchdog_thread.join(timeout=1.0)
        _watchdog_thread = None
    with _lock:
        _positions.clear()
        _in_flight.clear()
        _flatten_fail_counts.clear()
        _flatten_circuit_until.clear()
    _rest_client = None
    _last_coverage_reconcile_at = 0.0
    _stop.clear()


def adverse_points_against_position(
    *,
    epic: str,
    direction: str,
    entry_level: float,
    mid: float,
) -> float:
    """IG points price has moved against the position (0 if favorable)."""
    if entry_level <= 0 or mid <= 0:
        return 0.0
    delta = ig_points_to_price_delta(str(epic), 1.0)
    if delta <= 0:
        return 0.0
    d = str(direction or "").upper()
    if d == "BUY":
        return max(0.0, (float(entry_level) - float(mid)) / delta)
    return max(0.0, (float(mid) - float(entry_level)) / delta)


def on_streaming_mid_tick(epic: str, mid: float) -> None:
    """Hook from hub ingest — evaluate virtual ceiling on every fresh mid."""
    key = str(epic or "").strip()
    if not key or mid <= 0:
        return
    tracks: list[VirtualStopTrack] = []
    with _lock:
        for track in _positions.values():
            if track.epic == key and track.track_id not in _in_flight:
                tracks.append(track)
    for track in tracks:
        deal_key = str(track.deal_id or track.track_id)
        if flatten_circuit_open(deal_key):
            continue
        adverse = adverse_points_against_position(
            epic=track.epic,
            direction=track.direction,
            entry_level=track.entry_level,
            mid=float(mid),
        )
        if adverse >= track.ceiling_pts:
            _trigger_virtual_flatten(track, adverse_pts=adverse)


def _trigger_virtual_flatten(track: VirtualStopTrack, *, adverse_pts: float) -> None:
    deal_key = str(track.deal_id or track.track_id)
    with _lock:
        if track.track_id in _in_flight:
            return
        if _flatten_circuit_open_unlocked(deal_key):
            return
        _in_flight.add(track.track_id)
    log_engine(
        f"VirtualStop: CEILING BREACH epic={track.epic} adverse={adverse_pts:.2f}pt "
        f"≥ {track.ceiling_pts} — async market flatten"
    )
    try:
        from system.boot.boot_loop_holder import schedule_coro

        schedule_coro(_flatten_position_async(track))
    except Exception as exc:
        log_engine(f"VirtualStop: schedule_coro failed — sync fallback: {exc}")
        threading.Thread(
            target=_flatten_position_sync,
            args=(track,),
            name=f"virtual-stop-flatten-{track.track_id[:12]}",
            daemon=True,
        ).start()


async def _flatten_position_async(track: VirtualStopTrack) -> None:
    await asyncio.to_thread(_flatten_position_sync, track)


def _flatten_position_sync(track: VirtualStopTrack) -> None:
    """Flatten via exit gate with OPEN side (skip_lookup inverts once)."""
    rest = _rest_client
    deal_id = str(track.deal_id or track.track_id)
    ok = False
    if rest is None:
        clear_virtual_stop(track.track_id)
        return
    try:
        if not deal_id:
            for item in rest.open_positions() or []:
                market = item.get("market") or {}
                pos = item.get("position") or {}
                if str(market.get("epic") or "") != track.epic:
                    continue
                if str(pos.get("direction") or "").upper() == track.direction:
                    deal_id = str(pos.get("dealId") or pos.get("dealID") or "")
                    if deal_id:
                        break
        if deal_id:
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
                    reason="virtual_stop_ceiling",
                    source="virtual_stop",
                )
                ok = bool(result.get("ok") or result.get("already_flat"))
                if ok:
                    _clear_flatten_failure(deal_id)
                elif not result.get("skipped"):
                    _record_flatten_failure(
                        deal_id, detail=str(result.get("error") or "not_ok")
                    )
                log_engine(
                    f"VirtualStop: flatten via exit_gate epic={track.epic} "
                    f"deal={deal_id} ok={ok}"
                )
                return
            except Exception as gate_exc:
                log_engine(
                    f"VirtualStop: exit_gate failed — "
                    f"{type(gate_exc).__name__}: {gate_exc}"
                )
            # Legacy fallback: pass OPEN side with skip_lookup=True.
            open_side = str(track.direction or "BUY").upper()
            rest.close_position(
                deal_id,
                direction=open_side,
                size=track.size,
                epic=track.epic,
                budget_priority=True,
                skip_lookup=True,
                skip_confirm=True,
            )
            ok = True
            _clear_flatten_failure(deal_id)
        else:
            rest.flatten_epic_positions(track.epic)
            ok = True
        log_engine(
            f"VirtualStop: flatten dispatched epic={track.epic} deal={deal_id or 'epic-flat'}"
        )
    except Exception as exc:
        _record_flatten_failure(deal_id, detail=f"{type(exc).__name__}: {exc}")
        log_engine(
            f"VirtualStop: flatten failed epic={track.epic}: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        # On success or circuit-open: drop track. On soft failure keep cooled.
        if ok or flatten_circuit_open(deal_id):
            clear_virtual_stop(track.track_id)
            try:
                from runtime.dynamic_limit_engine import remove_track

                remove_track(deal_id or track.track_id)
            except Exception:
                pass
        else:
            with _lock:
                _in_flight.discard(track.track_id)


def _broker_mark_for_track(track: VirtualStopTrack) -> float | None:
    """IG bid/offer mark for virtual stop — avoids Yahoo hub scale mismatch."""
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
    if _quote_mark_trustworthy(track.entry_level, mid, track.epic):
        return mid
    return None


def _watchdog_loop() -> None:
    """500ms evaluation — IG marks first, hub fallback when scale matches entry."""
    while not _stop.is_set():
        try:
            with _lock:
                tracks = list(_positions.values())
            for track in tracks:
                if track.track_id in _in_flight:
                    continue
                mark = _broker_mark_for_track(track)
                if mark is None or mark <= 0:
                    continue
                on_streaming_mid_tick(track.epic, mark)
        except Exception:
            pass
        try:
            from runtime.dynamic_limit_engine import on_watchdog_tick as dyn_watchdog_tick
            from runtime.micro_gbp_exit import on_watchdog_tick

            on_watchdog_tick()
            dyn_watchdog_tick()
        except Exception:
            pass
        try:
            global _last_coverage_reconcile_at
            now = time.time()
            if _rest_client is not None and now - _last_coverage_reconcile_at >= 15.0:
                from execution.position_risk_stack import ensure_risk_stack_coverage

                ensure_risk_stack_coverage(_rest_client, force=False)
                _last_coverage_reconcile_at = now
        except Exception:
            pass
        _stop.wait(VIRTUAL_STOP_WATCHDOG_SEC)


def start_virtual_stop_watchdog(rest_client: Any | None) -> None:
    """Start 500ms internal risk watchdog (idempotent)."""
    global _watchdog_thread, _rest_client
    if rest_client is None:
        return
    _rest_client = rest_client
    with _lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _stop.clear()
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="virtual-stop-watchdog",
            daemon=True,
        )
        _watchdog_thread.start()
    log_engine(
        f"VirtualStop: watchdog online interval={VIRTUAL_STOP_WATCHDOG_SEC}s "
        f"internal_risk_ceiling={INTERNAL_RISK_CEILING_PTS}pt"
    )


def stop_virtual_stop_watchdog() -> None:
    global _watchdog_thread
    _stop.set()
    if _watchdog_thread is not None:
        _watchdog_thread.join(timeout=2.0)
        _watchdog_thread = None
