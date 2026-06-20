"""Live replay telemetry — tick rate, speed control, testbed win-rate snapshot."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

_lock = threading.RLock()
_speed = float(os.environ.get("IG_REPLAY_SPEED", "100"))
_total_ticks = 0
_tick_times: deque[float] = deque(maxlen=500)
_active_replayer: Any | None = None


@dataclass(frozen=True)
class ReplayTelemetry:
    speed: float
    total_ticks: int
    ticks_per_sec: float
    win_rate: float | None
    closed_trades: int
    wins: int
    losses: int
    replay_active: bool


def register_replayer(replayer: Any) -> None:
    global _active_replayer
    with _lock:
        _active_replayer = replayer


def unregister_replayer(replayer: Any) -> None:
    global _active_replayer
    with _lock:
        if _active_replayer is replayer:
            _active_replayer = None


def record_tick(*, epic: str = "") -> None:
    del epic
    global _total_ticks
    now = time.monotonic()
    with _lock:
        _total_ticks += 1
        _tick_times.append(now)


def get_speed() -> float:
    with _lock:
        return float(_speed)


def set_speed(speed: float) -> float:
    global _speed
    val = max(0.001, float(speed))
    with _lock:
        _speed = val
        os.environ["IG_REPLAY_SPEED"] = str(val)
        replayer = _active_replayer
    if replayer is not None:
        try:
            replayer._speed = val
        except Exception:
            pass
    return val


def _ticks_per_sec() -> float:
    with _lock:
        times = list(_tick_times)
    if len(times) < 2:
        return 0.0
    window = times[-1] - times[0]
    if window <= 0:
        return float(len(times))
    return max(0.0, (len(times) - 1) / window)


def _ledger_win_rate() -> tuple[float | None, int, int, int]:
    try:
        from system.testbed_firewall import is_testbed_firewall_active, testbed_ledger_path

        if not is_testbed_firewall_active():
            return None, 0, 0, 0
        path = testbed_ledger_path()
        if not path.is_file():
            return None, 0, 0, 0
        with sqlite3.connect(str(path)) as conn:
            rows = conn.execute(
                """
                SELECT result, ig_pnl_currency, pnl_points
                FROM trades
                WHERE closed_at IS NOT NULL AND closed_at != ''
                """
            ).fetchall()
        wins = losses = 0
        for result, ig_pnl, pnl_pts in rows:
            res = str(result or "").upper()
            pnl = ig_pnl if ig_pnl is not None else pnl_pts
            pnl_f = float(pnl or 0)
            if res == "WIN" or pnl_f > 0:
                wins += 1
            elif res == "LOSS" or pnl_f < 0:
                losses += 1
        total = wins + losses
        if total <= 0:
            return None, 0, wins, losses
        return wins / total, total, wins, losses
    except Exception:
        return None, 0, 0, 0


def get_telemetry() -> ReplayTelemetry:
    try:
        from simulation.replay_clock import is_replay_active

        replay_active = is_replay_active()
    except Exception:
        replay_active = False
    win_rate, closed, wins, losses = _ledger_win_rate()
    with _lock:
        total = _total_ticks
        speed = _speed
    return ReplayTelemetry(
        speed=speed,
        total_ticks=total,
        ticks_per_sec=round(_ticks_per_sec(), 1),
        win_rate=win_rate,
        closed_trades=closed,
        wins=wins,
        losses=losses,
        replay_active=replay_active,
    )


def telemetry_dict() -> dict[str, Any]:
    try:
        from simulation.replay_clock import is_replay_active, now_datetime
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        hardened = get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED
    except Exception:
        hardened = False
        is_replay_active = lambda: False  # noqa: E731
        now_datetime = lambda: None  # noqa: E731

    telem = get_telemetry()
    virtual_hms = None
    virtual_iso = None
    if is_replay_active():
        try:
            dt = now_datetime()
            virtual_hms = dt.strftime("%H:%M:%S")
            virtual_iso = dt.isoformat()
        except Exception:
            pass
    return {
        "hardened_testbed": hardened,
        "replay_active": telem.replay_active,
        "virtual_time": virtual_iso,
        "virtual_time_hms": virtual_hms,
        "ticks_per_sec": telem.ticks_per_sec,
        "replay_speed": telem.speed,
        "total_ticks": telem.total_ticks,
        "win_rate": telem.win_rate,
        "win_rate_pct": round(telem.win_rate * 100, 1) if telem.win_rate is not None else None,
        "closed_trades": telem.closed_trades,
        "wins": telem.wins,
        "losses": telem.losses,
    }
