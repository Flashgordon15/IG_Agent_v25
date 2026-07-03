"""
Technical indicators — v30 Apex vectorized NumPy core.

Pandas Series wrappers preserve v29.1 SignalEngine compatibility. Hot-path arrays
target <250µs per tick via ``compute_last_bar_indicators`` / ``compute_math_matrix``.
All matrix math uses ``dtype=np.float64`` — no Pandas on the calculation path.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

RSI_MIN_HISTORY_BARS = 15
ML_VETO_FLOOR = 0.450
ATR_CHANNEL_MULT = 2.0
_RSI_NEUTRAL = 50.0
_RSI_CLIP_LO = 15.0
_RSI_CLIP_HI = 85.0
_FLOAT64 = np.float64

# Spec-locked entry/exit thresholds — fixed constants, never fit on the full
# evaluation series (anti curve-fit). Percentile/regime windows cap lookback
# and exclude the bar being scored (leave-one-out).
SPEC_LOCKED_ENTRY_THRESHOLDS = True
VOL_REGIME_MIN_PRIOR_BARS = 10
VOL_REGIME_MAX_REF_BARS = 100
MICRO_TREND_MAX_FLOW_BOOST_PCT = 25.0
# Recent-first direction deadbands — tighter on the 3-tick leg to cut lag vs
# the slower multi-window mean fallback.
DIRECTION_DEADBAND_RECENT_PCT = 0.008
DIRECTION_DEADBAND_MEAN_PCT = 0.015

try:
    import apex_math  # type: ignore[import-untyped]  # optional Rust PyO3 extension

    _RUST_AVAILABLE = True
except ImportError:
    apex_math = None  # type: ignore[assignment]
    _RUST_AVAILABLE = False


def _as_float64(values: pd.Series | np.ndarray) -> np.ndarray:
    """Coerce any input to a contiguous float64 ndarray (hot-path safe)."""
    if isinstance(values, pd.Series):
        return np.ascontiguousarray(values.to_numpy(dtype=_FLOAT64, copy=False))
    return np.ascontiguousarray(np.asarray(values, dtype=_FLOAT64))


def resolve_ml_veto_floor(*, epic: str = "") -> float:
    """v30 hard-locked ML veto floor — apex.hardening authority (0.450)."""
    _ = epic
    from apex.hardening import ML_VETO_FLOOR

    return float(ML_VETO_FLOOR)


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing (RMA) on float64 — vectorized seed + tight tail loop."""
    n = len(values)
    out = np.full(n, np.nan, dtype=_FLOAT64)
    if n < period or period < 1:
        return out
    out[period - 1] = float(np.mean(values[:period], dtype=_FLOAT64))
    alpha = _FLOAT64(1.0) / _FLOAT64(period)
    for i in range(period, n):
        out[i] = out[i - 1] + alpha * (values[i] - out[i - 1])
    return out


def _np_ema(values: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average — pandas C-core ewm (vectorized, float64)."""
    n = len(values)
    if n == 0:
        return np.array([], dtype=_FLOAT64)
    span = max(1, int(span))
    return (
        pd.Series(values, dtype=float)
        .ewm(span=span, adjust=False, min_periods=1)
        .mean()
        .to_numpy(dtype=_FLOAT64, copy=False)
    )


def _np_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI on float64 close matrix — clipped to production bounds."""
    n = len(close)
    out = np.full(n, _RSI_NEUTRAL, dtype=_FLOAT64)
    if n < (period + 1):
        return out
    if _RUST_AVAILABLE and apex_math is not None:
        try:
            rust_out = apex_math.rsi_wilder(close, int(period))
            arr = np.asarray(rust_out, dtype=_FLOAT64)
            if arr.shape == close.shape:
                return np.clip(arr, _RSI_CLIP_LO, _RSI_CLIP_HI)
        except Exception:
            pass
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0.0, delta, 0.0).astype(_FLOAT64, copy=False)
    losses = np.where(delta < 0.0, -delta, 0.0).astype(_FLOAT64, copy=False)
    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)
    rs = np.divide(
        avg_gain,
        avg_loss,
        out=np.full(n, np.nan, dtype=_FLOAT64),
        where=avg_loss > 0.0,
    )
    rsi_vals = _FLOAT64(100.0) - (_FLOAT64(100.0) / (_FLOAT64(1.0) + rs))
    rsi_vals = np.where(np.isfinite(rsi_vals), rsi_vals, _RSI_NEUTRAL)
    return np.clip(rsi_vals, _RSI_CLIP_LO, _RSI_CLIP_HI)


def _np_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Average true range — float64 matrix only."""
    n = len(close)
    if n == 0:
        return np.array([], dtype=_FLOAT64)
    if _RUST_AVAILABLE and apex_math is not None:
        try:
            rust_out = apex_math.atr_wilder(high, low, close, int(period))
            arr = np.asarray(rust_out, dtype=_FLOAT64)
            if arr.shape == close.shape:
                return np.nan_to_num(arr, nan=0.0)
        except Exception:
            pass
    prev_close = np.empty(n, dtype=_FLOAT64)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )
    atr_vals = _wilder_smooth(tr.astype(_FLOAT64, copy=False), max(1, period))
    return np.nan_to_num(atr_vals, nan=0.0)


def _np_atr_channels(
    close: np.ndarray,
    atr: np.ndarray,
    *,
    mult: float = ATR_CHANNEL_MULT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ATR envelope channels — upper / mid / lower bands on float64 matrices."""
    m = _FLOAT64(mult)
    mid = close.astype(_FLOAT64, copy=False)
    band = atr.astype(_FLOAT64, copy=False) * m
    return mid + band, mid, mid - band


def _np_adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Average Directional Index — float64, Wilder-smoothed."""
    n = len(close)
    out = np.full(n, np.nan, dtype=_FLOAT64)
    if n < period + 2:
        return out
    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0).astype(_FLOAT64)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0).astype(_FLOAT64)
    prev_close = close[:-1]
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    ).astype(_FLOAT64)
    atr_s = _wilder_smooth(tr, period)
    plus_di = 100.0 * _wilder_smooth(plus_dm, period) / np.maximum(atr_s, 1e-12)
    minus_di = 100.0 * _wilder_smooth(minus_dm, period) / np.maximum(atr_s, 1e-12)
    dx = 100.0 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-12)
    adx_vals = _wilder_smooth(dx, period)
    out[period + 1 :] = adx_vals[period:]
    return np.nan_to_num(out, nan=0.0)


def build_validation_mask(
    n: int,
    rsi: np.ndarray,
    *,
    ml_probability: float | None = None,
    ml_veto_floor: float = ML_VETO_FLOOR,
) -> np.ndarray:
    """
    Production regime validation mask — history guard, RSI clip, ML veto floor.

    Returns bool ndarray length *n* (True = bar passes all guards).
    """
    if n <= 0:
        return np.array([], dtype=bool)
    idx = np.arange(n, dtype=np.int64)
    history_mask = idx >= (RSI_MIN_HISTORY_BARS - 1)
    rsi_ok = (rsi >= _RSI_CLIP_LO) & (rsi <= _RSI_CLIP_HI)
    if ml_probability is None:
        ml_ok = np.ones(n, dtype=bool)
    else:
        ml_ok = np.full(n, float(ml_probability) >= float(ml_veto_floor), dtype=bool)
    return history_mask & rsi_ok & ml_ok


def compute_math_matrix(
    close: np.ndarray,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    *,
    fast_span: int = 12,
    slow_span: int = 26,
    rsi_period: int = 14,
    atr_period: int = 14,
    ml_probability: float | None = None,
    ml_veto_floor: float | None = None,
    out_indicator_matrix: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    Worker B math matrix — full vectorized RSI / EMA / ATR channel stack.

    Optional *out_indicator_matrix* shape ``(n, 4)`` receives in-place writes
    (columns: rsi, ema_fast, ema_slow, atr) for zero-copy handoff to Worker C.
    """
    t0 = time.perf_counter()
    c = _as_float64(close)
    n = len(c)
    floor = float(ml_veto_floor if ml_veto_floor is not None else resolve_ml_veto_floor())

    empty: dict[str, Any] = {
        "close": c,
        "high": np.array([], dtype=_FLOAT64),
        "low": np.array([], dtype=_FLOAT64),
        "rsi": np.array([], dtype=_FLOAT64),
        "ema_fast": np.array([], dtype=_FLOAT64),
        "ema_slow": np.array([], dtype=_FLOAT64),
        "atr_arr": np.array([], dtype=_FLOAT64),
        "atr_upper_arr": np.array([], dtype=_FLOAT64),
        "atr_mid_arr": np.array([], dtype=_FLOAT64),
        "atr_lower_arr": np.array([], dtype=_FLOAT64),
        "validation_mask": np.array([], dtype=bool),
        "indicator_matrix": np.empty((0, 4), dtype=_FLOAT64),
        "ml_veto_floor": floor,
        "ml_pass": False,
        "history_ok": False,
        "fast_ema": 0.0,
        "slow_ema": 0.0,
        "atr": 0.0,
        "atr_upper": 0.0,
        "atr_lower": 0.0,
        "elapsed_us": 0.0,
    }

    if n == 0:
        empty["elapsed_us"] = (time.perf_counter() - t0) * 1_000_000.0
        return empty

    if high is not None and low is not None and len(high) == n and len(low) == n:
        h = _as_float64(high)
        lo = _as_float64(low)
    else:
        h = c.copy()
        lo = c.copy()

    min_bars = max(RSI_MIN_HISTORY_BARS, rsi_period + 1)
    if n < min_bars:
        ema_f = _np_ema(c, fast_span)
        ema_s = _np_ema(c, slow_span)
        rsi_arr = np.full(n, _RSI_NEUTRAL, dtype=_FLOAT64)
        atr_arr = np.zeros(n, dtype=_FLOAT64)
    else:
        ema_f = _np_ema(c, fast_span)
        ema_s = _np_ema(c, slow_span)
        rsi_arr = _np_rsi(c, rsi_period)
        atr_arr = _np_atr(h, lo, c, atr_period)

    atr_upper, atr_mid, atr_lower = _np_atr_channels(c, atr_arr)
    validation_mask = build_validation_mask(
        n, rsi_arr, ml_probability=ml_probability, ml_veto_floor=floor
    )

    if out_indicator_matrix is not None and out_indicator_matrix.shape[0] >= n:
        mat = out_indicator_matrix[:n]
        mat[:, 0] = rsi_arr
        mat[:, 1] = ema_f
        mat[:, 2] = ema_s
        mat[:, 3] = atr_arr
        indicator_matrix = mat
    else:
        indicator_matrix = np.column_stack((rsi_arr, ema_f, ema_s, atr_arr)).astype(
            _FLOAT64, copy=False
        )

    last_i = n - 1
    ml_pass = bool(validation_mask[last_i]) if n else False
    history_ok = n >= RSI_MIN_HISTORY_BARS

    elapsed_us = (time.perf_counter() - t0) * 1_000_000.0
    return {
        "close": c,
        "high": h,
        "low": lo,
        "rsi": rsi_arr,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "fast_ema": float(ema_f[last_i]),
        "slow_ema": float(ema_s[last_i]),
        "atr": float(atr_arr[last_i]),
        "atr_upper": float(atr_upper[last_i]),
        "atr_lower": float(atr_lower[last_i]),
        "atr_mid": float(atr_mid[last_i]),
        "atr_arr": atr_arr,
        "atr_upper_arr": atr_upper,
        "atr_mid_arr": atr_mid,
        "atr_lower_arr": atr_lower,
        "validation_mask": validation_mask,
        "indicator_matrix": indicator_matrix,
        "ml_veto_floor": floor,
        "ml_pass": ml_pass,
        "history_ok": history_ok,
        "elapsed_us": elapsed_us,
    }


def compute_last_bar_indicators(
    close: np.ndarray,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    *,
    fast_span: int = 12,
    slow_span: int = 26,
    rsi_period: int = 14,
    atr_period: int = 14,
    ml_probability: float | None = None,
    ml_veto_floor: float | None = None,
) -> dict[str, float]:
    """
    Ultra-fast last-bar indicator snapshot for streaming tick hot-path.

    Target: <250µs on typical 500-bar windows (NumPy core).
    """
    snap = compute_math_matrix(
        close,
        high,
        low,
        fast_span=fast_span,
        slow_span=slow_span,
        rsi_period=rsi_period,
        atr_period=atr_period,
        ml_probability=ml_probability,
        ml_veto_floor=ml_veto_floor,
    )
    n = len(snap["close"])
    if n == 0:
        return {
            "rsi": _RSI_NEUTRAL,
            "fast_ema": 0.0,
            "slow_ema": 0.0,
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "atr": 0.0,
            "atr_upper": 0.0,
            "atr_lower": 0.0,
            "ml_pass": False,
            "elapsed_us": snap["elapsed_us"],
        }
    rsi_arr = snap["rsi"]
    return {
        "rsi": float(rsi_arr[-1]) if len(rsi_arr) else _RSI_NEUTRAL,
        "fast_ema": snap["fast_ema"],
        "slow_ema": snap["slow_ema"],
        "ema_fast": snap["fast_ema"],
        "ema_slow": snap["slow_ema"],
        "atr": snap["atr"],
        "atr_upper": snap["atr_upper"],
        "atr_lower": snap["atr_lower"],
        "ml_pass": snap["ml_pass"],
        "elapsed_us": snap["elapsed_us"],
    }


def ema(series: pd.Series, span: int) -> pd.Series:
    values = _as_float64(series)
    if len(values) == 0:
        return pd.Series(dtype=float)
    out = _np_ema(values, span)
    return pd.Series(out, index=series.index, dtype=float)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder-style RSI on a price series.

    Requires at least ``period + 1`` observations; otherwise returns neutral 50.0
    for every row (prevents compressed garbage on thin tick/resample windows).
    """
    if series is None or len(series) < (period + 1):
        idx = series.index if series is not None and len(series) else pd.RangeIndex(0)
        return pd.Series(_RSI_NEUTRAL, index=idx, dtype=float)
    values = _as_float64(series)
    out = _np_rsi(values, period)
    return pd.Series(out, index=series.index, dtype=float)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if df.empty or not all(c in df.columns for c in ("high", "low", "close")):
        return pd.Series(dtype=float)
    high = _as_float64(df["high"])
    low = _as_float64(df["low"])
    close = _as_float64(df["close"])
    out = _np_atr(high, low, close, period)
    return pd.Series(out, index=df.index, dtype=float)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX trend-strength indicator on OHLC frame."""
    if df.empty or not all(c in df.columns for c in ("high", "low", "close")):
        return pd.Series(dtype=float)
    high = _as_float64(df["high"])
    low = _as_float64(df["low"])
    close = _as_float64(df["close"])
    out = _np_adx(high, low, close, period)
    return pd.Series(out, index=df.index, dtype=float)


def apply_indicators_frame(
    df: pd.DataFrame,
    *,
    fast_span: int = 12,
    slow_span: int = 26,
    rsi_period: int = 14,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Vectorized frame enrichment — drop-in for SignalEngine.add_indicators."""
    if df.empty:
        return df
    out = df.copy()
    price = _as_float64(out["price"])
    snap = compute_math_matrix(
        price,
        _as_float64(out["high"]) if "high" in out.columns else None,
        _as_float64(out["low"]) if "low" in out.columns else None,
        fast_span=fast_span,
        slow_span=slow_span,
        rsi_period=rsi_period,
        atr_period=atr_period,
    )
    n = len(price)
    if n < max(RSI_MIN_HISTORY_BARS, rsi_period + 1):
        out["rsi"] = _RSI_NEUTRAL
        out["fast_ema"] = out["price"]
        out["slow_ema"] = out["price"]
        out["atr"] = 0.0
    else:
        out["fast_ema"] = snap["ema_fast"]
        out["slow_ema"] = snap["ema_slow"]
        out["rsi"] = snap["rsi"]
        out["atr"] = snap["atr_arr"]
    return out


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


MICRO_TREND_SLICE_WINDOWS = (3, 5, 8)
STRATEGY_THRESHOLD_LOW_PCT = 42.0
STRATEGY_THRESHOLD_HIGH_PCT = 45.0
MICRO_TREND_FORECAST_TICKS = 4  # fractional 3–5 tick horizon (midpoint)
OBI_ALIGNMENT_THRESHOLD = 0.15
OFI_BOOST_SCALE = 35.0
OBI_BOOST_SCALE = 22.0
VELOCITY_BOOST_PCT = 8.0


def _prior_window_percentile_bounds(
    values: np.ndarray,
    *,
    low_pct: float,
    high_pct: float,
    min_prior: int = VOL_REGIME_MIN_PRIOR_BARS,
    max_ref: int = VOL_REGIME_MAX_REF_BARS,
) -> tuple[float, float, float] | None:
    """Leave-one-out percentile bounds — current bar excluded from reference."""
    finite = values[np.isfinite(values)]
    if finite.size < min_prior + 1:
        return None
    current = float(finite[-1])
    prior = finite[:-1]
    ref = prior[-min(max_ref, prior.size) :]
    if ref.size < min_prior:
        return None
    lo = float(np.percentile(ref, low_pct))
    hi = float(np.percentile(ref, high_pct))
    return current, lo, hi


def _endpoint_roc_windows(
    close: np.ndarray, windows: tuple[int, ...]
) -> tuple[np.ndarray, float, float]:
    """Vectorized endpoint RoC (%) for each lookback ending at close[-1]."""
    n = close.size
    end = float(close[-1])
    rocs = np.full(len(windows), np.nan, dtype=_FLOAT64)
    for i, w in enumerate(windows):
        if n <= w:
            continue
        base = float(close[-w])
        if base != 0.0:
            rocs[i] = (end - base) / abs(base) * 100.0
    recent_roc = 0.0
    prior_roc = 0.0
    if n >= 8:
        r0 = float(close[-3])
        p0 = float(close[-8])
        if r0 != 0.0:
            recent_roc = (end - r0) / abs(r0) * 100.0
        if p0 != 0.0:
            prior_roc = (float(close[-4]) - p0) / abs(p0) * 100.0
    return rocs, recent_roc, prior_roc


def _resolve_obi_ratio(
    obi_ratio: float | None,
    order_book_depth: Any | None,
) -> float:
    if obi_ratio is not None:
        try:
            return float(max(-1.0, min(1.0, obi_ratio)))
        except (TypeError, ValueError):
            return 0.0
    if order_book_depth is None:
        return 0.0
    try:
        from intelligence.order_book_imbalance import compute_obi_ratio

        return float(compute_obi_ratio(order_book_depth))
    except Exception:
        return 0.0


def _order_flow_aligned(direction: str, obi: float) -> bool:
    if direction == "BUY":
        return obi >= OBI_ALIGNMENT_THRESHOLD
    if direction == "SELL":
        return obi <= -OBI_ALIGNMENT_THRESHOLD
    return False


def _ofi_confirms_direction(direction: str, ofi_delta: float) -> bool:
    if direction == "BUY":
        return ofi_delta > 0.0
    if direction == "SELL":
        return ofi_delta < 0.0
    return False


def evaluate_micro_trend_alpha(
    close: np.ndarray,
    *,
    low_pct: float = STRATEGY_THRESHOLD_LOW_PCT,
    high_pct: float = STRATEGY_THRESHOLD_HIGH_PCT,
    obi_ratio: float | None = None,
    prior_obi_ratio: float | None = None,
    order_book_depth: Any | None = None,
    tick_velocity_engaged: bool = False,
) -> dict[str, Any]:
    """
    Localized micro-trend RoC + order-flow imbalance (OBI/OFI) + tick velocity.

    Blends price-velocity RoC with instantaneous OBI delta and 15-ticks/200ms
    velocity engagement to fractionally forecast the next 3–5 ticks of direction.
    Pure NumPy float64 on the price path — OBI resolved lazily when provided.
    """
    c = _as_float64(close)
    n = len(c)
    empty: dict[str, Any] = {
        "score_pct": 0.0,
        "roc_variance": 0.0,
        "promote": False,
        "promote_tier": "",
        "direction": "FLAT",
        "obi_ratio": 0.0,
        "ofi_delta": 0.0,
        "tick_velocity_engaged": bool(tick_velocity_engaged),
        "order_flow_aligned": False,
        "forecast_ticks": MICRO_TREND_FORECAST_TICKS,
        "forecast_direction": "FLAT",
        "forecast_confidence": 0.0,
    }
    if n < max(MICRO_TREND_SLICE_WINDOWS) + 1:
        return empty

    roc_arr, recent_roc, prior_roc = _endpoint_roc_windows(c, MICRO_TREND_SLICE_WINDOWS)
    valid = roc_arr[np.isfinite(roc_arr)]
    if valid.size == 0:
        return empty

    roc_var = float(np.var(valid))
    mean_roc = float(np.mean(valid))
    accel = recent_roc - prior_roc
    momentum = abs(recent_roc) + abs(accel) * 0.75 + roc_var * 4.0
    # Recent leg weighted higher than the multi-window mean — cuts entry lag.
    base_score = float(
        np.clip(
            abs(recent_roc) * 32.0 + abs(accel) * 22.0 + roc_var * 100.0,
            0.0,
            100.0,
        )
    )
    if base_score < momentum:
        base_score = float(np.clip(momentum * 14.0, 0.0, 100.0))

    direction = "FLAT"
    if recent_roc > DIRECTION_DEADBAND_RECENT_PCT:
        direction = "BUY"
    elif recent_roc < -DIRECTION_DEADBAND_RECENT_PCT:
        direction = "SELL"
    elif mean_roc > DIRECTION_DEADBAND_MEAN_PCT:
        direction = "BUY"
    elif mean_roc < -DIRECTION_DEADBAND_MEAN_PCT:
        direction = "SELL"

    obi = _resolve_obi_ratio(obi_ratio, order_book_depth)
    prior_obi = _resolve_obi_ratio(prior_obi_ratio, None) if prior_obi_ratio is not None else 0.0
    ofi_delta = float(obi - prior_obi) if prior_obi_ratio is not None else 0.0

    flow_aligned = _order_flow_aligned(direction, obi)
    ofi_confirms = _ofi_confirms_direction(direction, ofi_delta)
    flow_boost = 0.0
    if flow_aligned:
        flow_boost += abs(obi) * OBI_BOOST_SCALE
    if ofi_confirms:
        flow_boost += abs(ofi_delta) * OFI_BOOST_SCALE
    if tick_velocity_engaged:
        flow_boost += VELOCITY_BOOST_PCT
    flow_boost = min(flow_boost, MICRO_TREND_MAX_FLOW_BOOST_PCT)

    score_pct = float(np.clip(base_score + flow_boost, 0.0, 100.0))

    promote = score_pct >= low_pct and direction in ("BUY", "SELL")
    promote_tier = ""
    if promote:
        promote_tier = "high" if score_pct >= high_pct else "low"
        if flow_aligned and (ofi_confirms or tick_velocity_engaged):
            promote_tier = "high"

    forecast_dir = direction if promote else "FLAT"
    forecast_conf = 0.0
    if promote:
        velocity_factor = 1.12 if tick_velocity_engaged else 1.0
        flow_factor = 1.0 + min(0.35, abs(obi) * 0.4 + abs(ofi_delta) * 0.25)
        forecast_conf = float(
            np.clip((score_pct / 100.0) * velocity_factor * flow_factor, 0.0, 1.0)
        )

    return {
        "score_pct": score_pct,
        "base_score_pct": base_score,
        "roc_variance": roc_var,
        "mean_roc": mean_roc,
        "promote": promote,
        "promote_tier": promote_tier,
        "direction": direction,
        "obi_ratio": obi,
        "ofi_delta": ofi_delta,
        "tick_velocity_engaged": bool(tick_velocity_engaged),
        "order_flow_aligned": flow_aligned,
        "forecast_ticks": MICRO_TREND_FORECAST_TICKS,
        "forecast_direction": forecast_dir,
        "forecast_confidence": forecast_conf,
    }


def vol_regime(
    atr_series: pd.Series | np.ndarray,
    *,
    low_pct: float = 25.0,
    high_pct: float = 75.0,
) -> str:
    """Classify current ATR as 'low', 'normal', or 'high' using rolling percentiles."""
    try:
        if isinstance(atr_series, pd.Series):
            values = _as_float64(atr_series.dropna())
        else:
            values = _as_float64(np.asarray(atr_series, dtype=_FLOAT64))
        bounds = _prior_window_percentile_bounds(
            values[np.isfinite(values)],
            low_pct=low_pct,
            high_pct=high_pct,
        )
        if bounds is None:
            return "unknown"
        current, lo, hi = bounds
        if current <= lo:
            return "low"
        if current >= hi:
            return "high"
        return "normal"
    except Exception:
        return "unknown"
