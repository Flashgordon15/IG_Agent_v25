"""7-day Performance Points trajectory cache for Iron Ledger telemetry."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_WINDOW_DAYS = 7
_PP_EXPANSION_THRESHOLD = 1200
_PP_DEFENSE_THRESHOLD = 800


def _utc_day_key(ts: float | None = None) -> str:
    t = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return t.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class PPDailyPoint:
    day: str
    ts: float
    pp: int

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "ts": self.ts, "pp": self.pp}


class PPTimeHistoryCache:
    """Rolling 7-day settled PP coordinate matrix (timestamps vs scores)."""

    __slots__ = ("_lock", "_by_day", "_order")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_day: dict[str, PPDailyPoint] = {}
        self._order: deque[str] = deque(maxlen=_WINDOW_DAYS)

    def record_settled_pp(self, pp: int, *, ts: float | None = None) -> PPDailyPoint:
        """Log daily settled PP — overwrites same UTC day, trims to 7 days."""
        now = float(ts or time.time())
        day = _utc_day_key(now)
        point = PPDailyPoint(day=day, ts=now, pp=int(pp))
        with self._lock:
            self._by_day[day] = point
            if day not in self._order:
                self._order.append(day)
            while len(self._order) > _WINDOW_DAYS:
                evict = self._order.popleft()
                self._by_day.pop(evict, None)
        return point

    def touch_live_pp(self, pp: int, *, ts: float | None = None) -> None:
        """Update today's coordinate without waiting for day roll."""
        self.record_settled_pp(pp, ts=ts)

    def finalize_previous_day(self, pp: int, *, day: str, ts: float) -> None:
        """Explicit end-of-day settlement for harness simulations."""
        point = PPDailyPoint(day=day, ts=ts, pp=int(pp))
        with self._lock:
            self._by_day[day] = point
            if day not in self._order:
                self._order.append(day)
            while len(self._order) > _WINDOW_DAYS:
                evict = self._order.popleft()
                self._by_day.pop(evict, None)

    def _ordered_points(self) -> list[PPDailyPoint]:
        with self._lock:
            return [self._by_day[d] for d in self._order if d in self._by_day]

    def slope(self) -> float:
        pts = self._ordered_points()
        if len(pts) < 2:
            return 0.0
        return (pts[-1].pp - pts[0].pp) / max(1, len(pts) - 1)

    def trend_band(self) -> str:
        pts = self._ordered_points()
        if len(pts) < 2:
            return "neutral"
        latest = pts[-1].pp
        slope_val = self.slope()
        if slope_val > 0 and latest >= _PP_EXPANSION_THRESHOLD:
            return "expansion"
        if slope_val < 0 or latest <= _PP_DEFENSE_THRESHOLD:
            return "defense"
        return "neutral"

    def serialize_trajectory(self) -> dict[str, Any]:
        pts = self._ordered_points()
        timestamps = [p.ts for p in pts]
        pp_scores = [p.pp for p in pts]
        slope = self.slope()
        trend = self.trend_band()
        return {
            "ok": True,
            "window_days": _WINDOW_DAYS,
            "count": len(pts),
            "timestamps": timestamps,
            "pp_scores": pp_scores,
            "days": [p.day for p in pts],
            "slope": round(slope, 4),
            "trend": trend,
            "expansion_threshold": _PP_EXPANSION_THRESHOLD,
            "defense_threshold": _PP_DEFENSE_THRESHOLD,
            "latest_pp": pp_scores[-1] if pp_scores else 0,
        }


_cache = PPTimeHistoryCache()


def get_pp_history_cache() -> PPTimeHistoryCache:
    return _cache


def record_platform_pp_sample(pp: int, *, ts: float | None = None) -> None:
    _cache.touch_live_pp(pp, ts=ts)


def get_pp_trajectory_7d() -> dict[str, Any]:
    return _cache.serialize_trajectory()


def simulate_seven_day_pp_variation(
    scores: list[int],
    *,
    base_ts: float | None = None,
) -> dict[str, Any]:
    """Harness helper — inject synthetic daily PP settlements."""
    reset_pp_history_for_tests()
    anchor = float(base_ts or time.time()) - (len(scores) - 1) * 86400
    for i, pp in enumerate(scores):
        day = _utc_day_key(anchor + i * 86400)
        _cache.finalize_previous_day(pp, day=day, ts=anchor + i * 86400)
    return get_pp_trajectory_7d()


def reset_pp_history_for_tests() -> None:
    global _cache
    _cache = PPTimeHistoryCache()
