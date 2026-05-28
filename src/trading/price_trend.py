"""
30-minute mid-price trend for dashboard Live tab.

Uses 5m candles from signal-engine quote history when enough bars exist;
otherwise first vs last tick mid in the lookback window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from signals.indicators import floor_time

LOOKBACK_MINUTES = 30
CANDLE_MINUTES = 5
FLAT_POINTS = 15.0
FLAT_PCT = 0.0005  # 0.05%


def _candles_5m(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["time", "open", "close", "mid"])
    t = df.copy()
    t["bucket"] = t["time"].apply(lambda d: floor_time(d, CANDLE_MINUTES))
    return (
        t.groupby("bucket", as_index=False)
        .agg(
            time=("bucket", "first"),
            open=("mid", "first"),
            close=("mid", "last"),
            mid=("mid", "last"),
        )
        .sort_values("time")
        .reset_index(drop=True)
    )


def _classify_change(change_pts: float, ref_mid: float) -> str:
    change_pct = abs(change_pts) / ref_mid if ref_mid > 0 else 0.0
    if abs(change_pts) < FLAT_POINTS or change_pct < FLAT_PCT:
        return "flat"
    return "up" if change_pts > 0 else "down"


def compute_price_trend_30m(
    quote_df: pd.DataFrame,
    *,
    now: datetime | None = None,
    lookback_minutes: int = LOOKBACK_MINUTES,
) -> dict[str, Any] | None:
    """
    Return price trend over the last *lookback_minutes* (default 30).

    Keys: direction (up|flat|down), change_pts, change_pct, method (candles|ticks).
    None when insufficient quote history.
    """
    if quote_df is None or quote_df.empty or "mid" not in quote_df.columns:
        return None

    end = now or datetime.now()
    if getattr(end, "tzinfo", None) is not None:
        end = end.replace(tzinfo=None)

    df = quote_df.copy()
    if "time" not in df.columns:
        return None
    times = pd.to_datetime(df["time"], utc=False)
    if hasattr(times.dt, "tz") and times.dt.tz is not None:
        times = times.dt.tz_localize(None)
    df["time"] = times
    cutoff = end - timedelta(minutes=lookback_minutes)
    window = df[df["time"] >= cutoff].sort_values("time")
    if window.empty:
        window = df.sort_values("time")
    if len(window) < 2:
        return None

    candles = _candles_5m(window)
    ref_mid = 0.0
    end_mid = 0.0
    method = "ticks"

    if len(candles) >= 2:
        ref_mid = float(candles.iloc[0]["close"])
        end_mid = float(candles.iloc[-1]["close"])
        method = "candles"
    else:
        ref_mid = float(window.iloc[0]["mid"])
        end_mid = float(window.iloc[-1]["mid"])

    if ref_mid <= 0:
        return None

    change_pts = round(end_mid - ref_mid, 1)
    change_pct = round((change_pts / ref_mid) * 100.0, 3)
    direction = _classify_change(change_pts, ref_mid)

    return {
        "direction": direction,
        "change_pts": change_pts,
        "change_pct": change_pct,
        "method": method,
        "lookback_minutes": lookback_minutes,
    }
