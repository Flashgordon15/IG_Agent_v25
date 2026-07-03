"""
Sentiment Momentum Tracker — multi-horizon client positioning derivatives.

Records IG retail sentiment samples and computes 5-minute / 30-minute
first derivatives for ML feature ingestion and continuous environment scoring.
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_left
from collections import deque
from typing import Any

_HISTORY: dict[str, deque[tuple[float, float]]] = {}
_LOCK = threading.Lock()
_MAX_SAMPLES = 512
_WINDOW_5M_SEC = 300.0
_WINDOW_15M_SEC = 900.0
_WINDOW_30M_SEC = 1800.0
_WINDOW_1H_SEC = 3600.0


def reset_sentiment_momentum_for_tests() -> None:
    with _LOCK:
        _HISTORY.clear()


def record_sentiment_sample(epic: str, long_pct: float, *, ts: float | None = None) -> None:
    """Append a client positioning sample (long %)."""
    key = str(epic or "").strip()
    if not key:
        return
    sample_ts = float(ts if ts is not None else time.time())
    value = float(max(0.0, min(100.0, long_pct)))
    with _LOCK:
        hist = _HISTORY.setdefault(key, deque(maxlen=_MAX_SAMPLES))
        hist.append((sample_ts, value))


def _derivative(
    points: list[tuple[float, float]],
    timestamps: list[float],
    window_sec: float,
    now: float,
) -> float:
    """Endpoint slope over samples inside the window; timestamps are monotonic."""
    idx = bisect_left(timestamps, now - window_sec)
    if len(points) - idx < 2:
        return 0.0
    t0, v0 = points[idx]
    t1, v1 = points[-1]
    dt = max(t1 - t0, 1e-6)
    return float((v1 - v0) / dt)


def sentiment_momentum_features(epic: str, *, now: float | None = None) -> dict[str, float]:
    """
    Continuous sentiment surface for ML + environment scoring.

    Returns long_pct, delta_5m, delta_30m (per-second ROC), surface_score.
    """
    key = str(epic or "").strip()
    ts_now = float(now if now is not None else time.time())
    empty = {
        "long_pct": 50.0,
        "delta_5m": 0.0,
        "delta_15m": 0.0,
        "delta_30m": 0.0,
        "delta_1h": 0.0,
        "accel_5m": 0.0,
        "accel_15m": 0.0,
        "accel_1h": 0.0,
        "surface_score": 0.0,
        "contrarian_pressure": 0.0,
    }
    if not key:
        return empty
    with _LOCK:
        hist = _HISTORY.get(key)
        if not hist:
            return empty
        points = list(hist)
    long_pct = float(points[-1][1])
    timestamps = [t for t, _ in points]
    delta_5m = _derivative(points, timestamps, _WINDOW_5M_SEC, ts_now)
    delta_15m = _derivative(points, timestamps, _WINDOW_15M_SEC, ts_now)
    delta_30m = _derivative(points, timestamps, _WINDOW_30M_SEC, ts_now)
    delta_1h = _derivative(points, timestamps, _WINDOW_1H_SEC, ts_now)
    accel_5m = delta_5m - _derivative(points, timestamps, _WINDOW_5M_SEC, ts_now - _WINDOW_5M_SEC * 0.5)
    accel_15m = delta_15m - _derivative(points, timestamps, _WINDOW_15M_SEC, ts_now - _WINDOW_15M_SEC * 0.5)
    accel_1h = delta_1h - _derivative(points, timestamps, _WINDOW_1H_SEC, ts_now - _WINDOW_1H_SEC * 0.5)

    surface = 0.0
    if long_pct > 70.0:
        surface -= (long_pct - 70.0) * 0.12
    elif long_pct < 30.0:
        surface -= (30.0 - long_pct) * 0.12
    surface -= delta_5m * 180.0
    surface += delta_30m * 45.0
    surface = float(max(-20.0, min(12.0, surface)))

    contrarian = 0.0
    if long_pct > 75.0 and delta_5m > 0:
        contrarian = min(1.0, (long_pct - 75.0) / 25.0 + delta_5m * 120.0)
    elif long_pct < 25.0 and delta_5m < 0:
        contrarian = min(1.0, (25.0 - long_pct) / 25.0 + abs(delta_5m) * 120.0)

    return {
        "long_pct": long_pct,
        "delta_5m": round(delta_5m, 6),
        "delta_15m": round(delta_15m, 6),
        "delta_30m": round(delta_30m, 6),
        "delta_1h": round(delta_1h, 6),
        "accel_5m": round(accel_5m, 6),
        "accel_15m": round(accel_15m, 6),
        "accel_1h": round(accel_1h, 6),
        "surface_score": round(surface, 3),
        "contrarian_pressure": round(contrarian, 4),
    }


def sentiment_surface_adjustment(epic: str) -> dict[str, Any]:
    """Environment fitness modifier — replaces binary +/-10 with continuous surface."""
    feats = sentiment_momentum_features(epic)
    return {
        "value": feats["long_pct"],
        "adjustment": feats["surface_score"],
        "delta_5m": feats["delta_5m"],
        "delta_30m": feats["delta_30m"],
        "contrarian_pressure": feats["contrarian_pressure"],
        "label": (
            "crowded_long_momentum"
            if feats["long_pct"] > 75 and feats["delta_5m"] > 0
            else (
                "crowded_short_momentum"
                if feats["long_pct"] < 25 and feats["delta_5m"] < 0
                else (
                    "crowded_long"
                    if feats["long_pct"] > 80
                    else "crowded_short" if feats["long_pct"] < 20 else "neutral"
                )
            )
        ),
    }
