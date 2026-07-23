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
        from ml.interim_scorer import invalidate_ml_clean_training_rows_cache

        invalidate_ml_clean_training_rows_cache()
    except Exception as e:
        log_engine(
            f"ml_training_store entry hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )


def warm_ml_row_cache(cfg: Any | None = None) -> None:
    """Pre-load ML row count at boot and train model if enough labels exist."""
    try:
        from ml.interim_scorer import ml_clean_training_rows
        from system.config_loader import get_config

        active = cfg or get_config()
        ml_clean_training_rows(active)
        from ml.auto_trainer import train_model_from_store

        result = train_model_from_store(active)
        if result.get("ok"):
            log_engine(
                f"ml boot train: {result.get('labels')} labels "
                f"features={result.get('features')}"
            )
    except Exception as exc:
        log_engine(f"ml row cache warm skipped: {type(exc).__name__}: {exc}")


def record_ml_exit_for_deal(
    deal_id: str,
    *,
    ig_pnl: float,
    result: str,
    exit_price: float = 0.0,
    exit_reason: str = "ig_sync",
    pts_pnl: float = 0.0,
    points_scored: float = 0.0,
    profit_tier_pct: float | None = None,
    peak_pct_of_target: float | None = None,
    profit_pct_of_target: float | None = None,
    hold_sec: float | None = None,
    session_slot: str | None = None,
) -> None:
    try:
        if _ml_store is None:
            return
        gbp = float(ig_pnl)
        payload: dict[str, Any] = {
            "exit_price": float(exit_price),
            "exit_time": MLTrainingStore.iso_now(),
            "pts_pnl": float(pts_pnl),
            "gbp_pnl": gbp,
            "ig_pnl_currency": gbp if abs(gbp) >= 0.001 else None,
            "exit_reason": exit_reason,
            "result": result,
            "points_scored": float(points_scored),
            "confirmed": True,
            "source": "agent",
            "version": ML_VERSION,
        }
        if profit_tier_pct is not None:
            payload["profit_tier_pct"] = float(profit_tier_pct)
        if peak_pct_of_target is not None:
            payload["peak_pct_of_target"] = float(peak_pct_of_target)
        if profit_pct_of_target is not None:
            payload["profit_pct_of_target"] = float(profit_pct_of_target)
        if hold_sec is not None:
            payload["hold_sec"] = round(float(hold_sec), 1)
        if session_slot:
            payload["session_slot"] = str(session_slot)
        _ml_store.record_exit(deal_id, payload)
        from ml.interim_scorer import invalidate_ml_clean_training_rows_cache

        invalidate_ml_clean_training_rows_cache()
    except Exception as e:
        log_engine(
            f"ml_training_store exit hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )


def hydrate_ml_entry_from_deal(deal_id: str) -> dict[str, Any] | None:
    """Rebuild ML entry features from learning_db when the in-memory buffer was lost."""
    did = str(deal_id or "").strip()
    if not did:
        return None
    try:
        from data.learning_store import LearningStore
        from system.learning_trade_policy import is_ig_import_setup_key
        from system.paths import data_dir

        store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
        row = store.find_open_by_deal_id(did)
        if row is None:
            row = store.conn.execute(
                """
                SELECT * FROM trades
                WHERE ig_deal_id=? OR deal_reference=?
                ORDER BY id DESC LIMIT 1
                """,
                (did, did),
            ).fetchone()
        if row is None:
            try:
                from runtime.micro_gbp_exit import snapshot as gbp_snap

                track = (gbp_snap().get("tracks") or {}).get(did)
                if track:
                    return {
                        "confidence": 55.0,
                        "adjusted_score": 55.0,
                        "raw_score": 55.0,
                        "fired": 1,
                        "stop_pts": 4.0,
                        "confidence_band": "marginal",
                        "setup_name": "managed_exit_hydrate",
                        "trend_bias": "mixed",
                        "rsi": 50.0,
                        "atr": 1.0,
                        "spread": 0.0,
                        "daily_range_ratio": 0.0,
                        "limit_to_atr_ratio": 0.0,
                        "limit_pts": 0.0,
                        "net_profit_pts": 0.0,
                        "volume_regime": "unknown",
                        "session_window": "",
                        "signal_mid": float(track.get("entry_level") or 0.0),
                        "entry_price": float(track.get("entry_level") or 0.0),
                        "slippage_pts": 0.0,
                        "entry_time": MLTrainingStore.iso_now(),
                        "fitness_score": 0.0,
                        "points_state": "HEALTHY",
                        "size_multiplier": 1.0,
                        "instrument": str(track.get("epic") or ""),
                        "version": ML_VERSION,
                        "source": "gbp_track_hydrate",
                    }
            except Exception:
                pass
            return None
        keys = row.keys()
        setup = str(row["setup_key"] if "setup_key" in keys else "")
        if is_ig_import_setup_key(setup):
            return None
        conf = float(
            row["adjusted_confidence"]
            if "adjusted_confidence" in keys and row["adjusted_confidence"]
            else row["confidence"]
            if "confidence" in keys
            else 0.0
        )
        size = float(row["size"] if "size" in keys else 1.0) or 1.0
        entry_px = float(row["entry"] if "entry" in keys else 0.0)
        epic = str(row["epic"] if "epic" in keys else "")
        market = str(row["market"] if "market" in keys else epic)
        opened = str(row["opened_at"] if "opened_at" in keys else "")
        notes = str(row["notes"] if "notes" in keys else "")
        rsi = 50.0
        atr = 1.0
        spread = 0.0
        for token in notes.replace(",", " ").split():
            if token.startswith("rsi="):
                try:
                    rsi = float(token.split("=", 1)[1])
                except ValueError:
                    pass
            elif token.startswith("atr="):
                try:
                    atr = float(token.split("=", 1)[1])
                except ValueError:
                    pass
            elif token.startswith("spread="):
                try:
                    spread = float(token.split("=", 1)[1])
                except ValueError:
                    pass
        band = "marginal"
        if _points_engine is not None:
            try:
                band = _points_engine.confidence_band(conf)
            except Exception:
                pass
        return {
            "confidence": conf,
            "adjusted_score": conf,
            "raw_score": conf,
            "fired": 1,
            "stop_pts": 4.0,
            "confidence_band": band,
            "setup_name": setup,
            "trend_bias": "mixed",
            "rsi": rsi,
            "atr": atr,
            "spread": spread,
            "daily_range_ratio": (spread / atr) if atr > 0 else 0.0,
            "limit_to_atr_ratio": 0.0,
            "limit_pts": 0.0,
            "net_profit_pts": 0.0,
            "volume_regime": "unknown",
            "session_window": "",
            "signal_mid": entry_px,
            "entry_price": entry_px,
            "slippage_pts": 0.0,
            "entry_time": opened or MLTrainingStore.iso_now(),
            "fitness_score": 0.0,
            "points_state": "HEALTHY",
            "size_multiplier": size,
            "instrument": market,
            "version": ML_VERSION,
            "source": "agent",
        }
    except Exception as exc:
        log_engine(
            f"hydrate_ml_entry_from_deal failed deal={did}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None
