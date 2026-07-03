"""
Dynamic spread-widening forecast — rolling Z-score on spread level and delta.

Detects IG spread manipulation / widening during news or low-liquidity windows.
Pure math; no I/O. Heavy batch recompute runs in IntelligenceComputeWorker.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from intelligence.types import SpreadForecastVerdict

DEFAULT_WINDOW = 120
DEFAULT_Z_THRESHOLD = 2.5
DEFAULT_DELTA_Z_THRESHOLD = 2.0
DEFAULT_MIN_SAMPLES = 20


@dataclass
class _SpreadSeries:
    spreads: deque[float]
    last_spread: float = 0.0


class SpreadWideningForecast:
    """
    Streaming rolling-Z-score model on spread level and spread delta.

    throttle_factor ∈ [0, 1]: 0 = full size, 1 = halt new entries.
    offset_widen_pts: suggested limit/stop distance widening in IG points.
    """

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        z_threshold: float = DEFAULT_Z_THRESHOLD,
        delta_z_threshold: float = DEFAULT_DELTA_Z_THRESHOLD,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        max_throttle: float = 0.85,
        max_offset_widen_pts: float = 8.0,
    ) -> None:
        self._window = max(10, int(window))
        self._z_threshold = float(z_threshold)
        self._delta_z_threshold = float(delta_z_threshold)
        self._min_samples = max(5, int(min_samples))
        self._max_throttle = min(1.0, max(0.0, float(max_throttle)))
        self._max_offset_widen = max(0.0, float(max_offset_widen_pts))
        self._lock = threading.Lock()
        self._series: dict[str, _SpreadSeries] = {}

    def record(self, epic: str, spread: float) -> float:
        """Append spread sample; returns spread delta vs previous tick."""
        key = str(epic or "").strip()
        if not key or spread <= 0:
            return 0.0
        with self._lock:
            row = self._series.get(key)
            if row is None:
                row = _SpreadSeries(spreads=deque(maxlen=self._window))
                self._series[key] = row
            delta = float(spread) - float(row.last_spread) if row.last_spread > 0 else 0.0
            row.spreads.append(float(spread))
            row.last_spread = float(spread)
            return delta

    @staticmethod
    def _z_score(value: float, mean: float, std: float) -> float:
        if std <= 1e-12:
            return 0.0
        return (value - mean) / std

    def compute(self, epic: str) -> SpreadForecastVerdict:
        key = str(epic or "").strip()
        with self._lock:
            row = self._series.get(key)
            if row is None or len(row.spreads) < 2:
                return SpreadForecastVerdict(
                    epic=key,
                    spread=0.0,
                    spread_delta=0.0,
                    z_score=0.0,
                    delta_z_score=0.0,
                    mean_spread=0.0,
                    std_spread=0.0,
                    throttle_factor=0.0,
                    offset_widen_pts=0.0,
                    blocked=False,
                    reason="insufficient_samples",
                )
            spreads = np.asarray(row.spreads, dtype=np.float64)

        n = int(spreads.size)
        current = float(spreads[-1])
        delta = current - float(spreads[-2])

        # Leave-one-out reference stats: the scored sample is excluded from its
        # own mean/std window so a genuine spike cannot inflate sigma and mask itself.
        prior = spreads[:-1]
        mean = float(prior.mean())
        std = float(prior.std(ddof=1)) if prior.size >= 2 else 0.0

        prior_deltas = np.diff(prior)
        if prior_deltas.size >= 2:
            d_mean = float(prior_deltas.mean())
            d_std = float(prior_deltas.std(ddof=1))
        else:
            d_mean = 0.0
            d_std = 0.0

        z = self._z_score(current, mean, std)
        dz = self._z_score(delta, d_mean, d_std)

        blocked = False
        throttle = 0.0
        offset = 0.0
        reason = ""

        if n >= self._min_samples:
            level_breach = z >= self._z_threshold
            delta_breach = dz >= self._delta_z_threshold and delta > 0
            if level_breach or delta_breach:
                severity = max(
                    z / max(self._z_threshold, 1e-6),
                    dz / max(self._delta_z_threshold, 1e-6) if delta_breach else 0.0,
                )
                throttle = min(self._max_throttle, 0.25 + 0.2 * severity)
                offset = min(self._max_offset_widen, 2.0 + 1.5 * max(z, dz))
                blocked = severity >= 1.8
                parts = []
                if level_breach:
                    parts.append(f"spread z={z:.2f}")
                if delta_breach:
                    parts.append(f"delta z={dz:.2f}")
                reason = "spread_widening_forecast: " + ", ".join(parts)

        return SpreadForecastVerdict(
            epic=key,
            spread=current,
            spread_delta=delta,
            z_score=z,
            delta_z_score=dz,
            mean_spread=mean,
            std_spread=std,
            throttle_factor=throttle,
            offset_widen_pts=offset,
            blocked=blocked,
            reason=reason,
        )

    def snapshot(self, epic: str) -> dict[str, Any]:
        verdict = self.compute(epic)
        return {
            "epic": verdict.epic,
            "spread": verdict.spread,
            "z_score": verdict.z_score,
            "delta_z_score": verdict.delta_z_score,
            "throttle_factor": verdict.throttle_factor,
            "offset_widen_pts": verdict.offset_widen_pts,
            "blocked": verdict.blocked,
            "reason": verdict.reason,
        }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._series.clear()
