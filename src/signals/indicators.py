"""Technical indicators migrated from v22."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if df.empty or not all(c in df.columns for c in ("high", "low", "close")):
        return pd.Series(dtype=float)
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def floor_time(dt: datetime, minutes: int) -> datetime:
    return dt - timedelta(
        minutes=dt.minute % minutes,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )


def session_name(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    hour = dt.hour
    if 0 <= hour < 7:
        return "asia_early"
    if 7 <= hour < 12:
        return "london_morning"
    if 12 <= hour < 16:
        return "london_us_overlap"
    if 16 <= hour < 22:
        return "us_afternoon"
    return "late"


def bucket(value: float, step: float, cap: float = 9999) -> str:
    try:
        v = max(0, min(float(value), cap))
        lo = int(v // step * step)
        hi = int(lo + step)
        return f"{lo}-{hi}"
    except Exception:
        return "unknown"


def vol_regime(atr_series: pd.Series, *, low_pct: float = 25.0, high_pct: float = 75.0) -> str:
    """Classify current ATR as 'low', 'normal', or 'high' using rolling percentiles.

    Uses the last 100 values of *atr_series* as the reference distribution.
    Returns 'unknown' when there are fewer than 10 observations.
    """
    try:
        values = atr_series.dropna()
        if len(values) < 10:
            return "unknown"
        ref = values.iloc[-min(100, len(values)):]
        current = float(values.iloc[-1])
        lo = float(np.percentile(ref, low_pct))
        hi = float(np.percentile(ref, high_pct))
        if current <= lo:
            return "low"
        if current >= hi:
            return "high"
        return "normal"
    except Exception:
        return "unknown"
