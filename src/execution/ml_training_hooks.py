"""Wire MLTrainingStore to live entry/exit without blocking execution."""

from __future__ import annotations

from typing import Any

from data.ml_training_store import MLTrainingStore, ML_VERSION
from execution.types import TradeSignal
from signals.indicators import session_name
from system.engine_log import log_engine

_ml_store: MLTrainingStore | None = None
_points_engine: Any | None = None
_environment_scorer: Any | None = None


def get_points_engine():
    """Return the live PointsEngine instance wired at bootstrap.
    Returns None if configure_ml_training has not been called yet."""
    return _points_engine


def configure_ml_training(
    *,
    ml_store: MLTrainingStore | None = None,
    points_engine: Any | None = None,
    environment_scorer: Any | None = None,
) -> None:
    global _ml_store, _points_engine, _environment_scorer
    _ml_store = ml_store
    _points_engine = points_engine
    _environment_scorer = environment_scorer
    log_engine(
        f"ml_training: configured — "
        f"points_engine={'wired' if points_engine is not None else 'None'}"
    )


def _snapshot_field(snapshot: dict[str, Any], key: str, default: Any = 0.0) -> Any:
    last = snapshot.get("last")
    if last is not None and hasattr(last, "get"):
        try:
            return last.get(key, default)
        except Exception:
            pass
    return snapshot.get(key, default)


def record_ml_entry_from_signal(
    deal_id: str,
    signal: TradeSignal,
    execution_params: dict[str, Any],
    *,
    fill_price: float | None = None,
) -> None:
    try:
        if _ml_store is None:
            return
        conf = float(signal.adjusted_confidence)
        snap = signal.snapshot or {}
        trend15 = snap.get("trend15")
        trend = "mixed"
        if trend15 is not None and hasattr(trend15, "get"):
            if float(trend15.get("fast_ema", 0)) > float(trend15.get("slow_ema", 0)):
                trend = "bull"
            elif float(trend15.get("fast_ema", 0)) < float(trend15.get("slow_ema", 0)):
                trend = "bear"
        points = _points_engine
        band = points.confidence_band(conf) if points is not None else "marginal"
        fitness = 0.0
        if _environment_scorer is not None:
            try:
                fitness = float(_environment_scorer.score(signal.market, quote=signal.quote))
            except Exception:
                fitness = 0.0
        mult = (
            float(points.get_size_multiplier(conf))
            if points is not None
            else float(execution_params.get("size", 1.0))
        )
        signal_mid = float(signal.quote.mid)
        entry_px = float(fill_price if fill_price is not None else signal_mid)
        # Positive = unfavourable slip (paid more than mid for BUY / received less for SELL)
        slippage_pts = abs(entry_px - signal_mid)
        raw_conf = float(snap.get("raw_confidence", conf))
        # execution_params key for stop distance is "risk" (set by ExecutionEngine.execute_trade)
        stop_pts = float(execution_params.get("risk", execution_params.get("stop_distance", 0.0)) or 0.0)
        limit_pts = float(execution_params.get("limit", 0.0) or 0.0)
        atr_val = float(_snapshot_field(snap, "atr", 0.0))
        spread_val = float(signal.quote.spread)
        # daily_range_ratio: spread cost as fraction of ATR (daily range proxy).
        # High ratio → spread eats a large % of achievable move → poor expectancy.
        daily_range_ratio = (spread_val / atr_val) if atr_val > 0 else 0.0
        # limit_to_atr_ratio: how realistic the profit target is vs recent volatility.
        limit_to_atr_ratio = (limit_pts / atr_val) if atr_val > 0 and limit_pts > 0 else 0.0
        _ml_store.record_entry(
            deal_id,
            {
                "confidence": conf,
                # Core model features (must match ml_scorer feature_names)
                "adjusted_score": conf,
                "raw_score": raw_conf,
                "fired": 1,
                "stop_pts": stop_pts,
                "confidence_band": band,
                "setup_name": signal.setup_key,
                "trend_bias": trend,
                "rsi": float(_snapshot_field(snap, "rsi", 0.0)),
                "atr": atr_val,
                "spread": spread_val,
                # Range/spread awareness features
                "daily_range_ratio": daily_range_ratio,
                "limit_to_atr_ratio": limit_to_atr_ratio,
                "limit_pts": limit_pts,
                "net_profit_pts": limit_pts - spread_val,
                "volume_regime": str(snap.get("vol_regime") or "unknown"),
                "session_window": session_name(signal.quote.time),
                "signal_mid": signal_mid,
                "entry_price": entry_px,
                "slippage_pts": slippage_pts,
                "entry_time": MLTrainingStore.iso_now(),
                "fitness_score": fitness,
                "points_state": points.get_state() if points is not None else "HEALTHY",
                "size_multiplier": mult,
                "instrument": signal.market,
                "version": ML_VERSION,
            },
        )
    except Exception as e:
        log_engine(
            f"ml_training_store entry hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )


def record_ml_exit_for_deal(
    deal_id: str,
    *,
    ig_pnl: float,
    result: str,
    exit_price: float = 0.0,
    exit_reason: str = "ig_sync",
    pts_pnl: float = 0.0,
    points_scored: float = 0.0,
) -> None:
    try:
        if _ml_store is None:
            return
        _ml_store.record_exit(
            deal_id,
            {
                "exit_price": float(exit_price),
                "exit_time": MLTrainingStore.iso_now(),
                "pts_pnl": float(pts_pnl),
                "gbp_pnl": float(ig_pnl),
                "ig_pnl_currency": float(ig_pnl),
                "exit_reason": exit_reason,
                "result": result,
                "points_scored": float(points_scored),
                "confirmed": True,
                "source": "agent",
                "version": ML_VERSION,
            },
        )
    except Exception as e:
        log_engine(
            f"ml_training_store exit hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )
