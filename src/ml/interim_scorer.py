"""
Interim rules-based confidence scorer — active until ML store reaches threshold.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from signals.indicators import session_name
from system.config import Config
from system.engine_log import log_engine


@dataclass
class InterimScore:
    total: float
    trend: float
    session: float
    volatility: float
    recent_performance: float
    notes: str


def extract_live_state_vector(
    market: str,
    quote: Any,
    points_state: Any,
) -> dict[str, float | str | None]:
    """
    Lightweight, non-blocking in-RAM feature extraction.

    Returns a small dict describing live conditions that gates can use
    (spread / quote age / ATR multiplier / session score context).

    Rule: this function must not touch disk or SQLite in the hot path.
    """

    # --- Spread (bid/ask) ---
    spread = 0.0
    try:
        if hasattr(quote, "spread"):
            spread = float(getattr(quote, "spread") or 0.0)
        else:
            bid = float(getattr(quote, "bid", 0.0) or 0.0)
            offer = float(getattr(quote, "offer", 0.0) or 0.0)
            spread = max(0.0, offer - bid)
    except Exception:
        spread = 0.0

    # --- Quote age ---
    quote_age_s = 0.0
    try:
        q_time = getattr(quote, "time", None)
        if q_time is None and isinstance(quote, dict):
            q_time = quote.get("time")
        if isinstance(q_time, datetime):
            qt = q_time
            if qt.tzinfo is None:
                qt = qt.replace(tzinfo=timezone.utc)
            quote_age_s = max(0.0, (datetime.now(timezone.utc) - qt).total_seconds())
    except Exception:
        quote_age_s = 0.0

    # --- ATR multiplier (dimensionless) ---
    # Convention: callers pass atr_multiplier / atr_ratio via points_state dict.
    atr_multiplier = 0.0
    try:
        if isinstance(points_state, dict):
            atr_multiplier = float(
                points_state.get("atr_multiplier")
                or points_state.get("atr_ratio")
                or points_state.get("atr_mult")
                or 0.0
            )
        else:
            atr_multiplier = float(
                getattr(points_state, "atr_multiplier", None)
                or getattr(points_state, "atr_ratio", None)
                or getattr(points_state, "atr_mult", None)
                or 0.0
            )
    except Exception:
        atr_multiplier = 0.0

    # --- Active session score context ---
    session_score = 0.0
    nominal_state: str | None = None
    try:
        if isinstance(points_state, dict):
            session_score = float(
                points_state.get("session_score")
                or points_state.get("cumulative")
                or 0.0
            )
            nominal_state = (
                points_state.get("nominal_state")
                or points_state.get("points_state")
                or points_state.get("state")
            )
        else:
            session_score = float(
                getattr(points_state, "session_score", None)
                or getattr(points_state, "cumulative", None)
                or 0.0
            )
            nominal_state = (
                getattr(points_state, "nominal_state", None)
                or getattr(points_state, "points_state", None)
                or getattr(points_state, "state", None)
            )
    except Exception:
        session_score = 0.0
        nominal_state = None

    return {
        "market": str(market or ""),
        "spread": float(spread),
        "quote_age_s": float(quote_age_s),
        "atr_multiplier": float(atr_multiplier),
        "session_score": float(session_score),
        "session_nominal_state": str(nominal_state) if nominal_state else None,
    }


def _weights(cfg: Config) -> dict[str, float]:
    raw = cfg.get("interim_scorer_weights")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "trend": float(raw.get("trend", 25)),
        "session": float(raw.get("session", 25)),
        "volatility": float(raw.get("volatility", 25)),
        "recent_performance": float(raw.get("recent_performance", 25)),
    }


def ml_min_rows_for_model(cfg: Config) -> int:
    ep = cfg.get("entry_protection")
    if isinstance(ep, dict) and "ml_min_rows_for_trust" in ep:
        return int(ep.get("ml_min_rows_for_trust", 50))
    return int(cfg.get("ml_min_rows_for_model", 50))


def ml_training_rows() -> int:
    try:
        from data.ml_training_store import MLTrainingStore

        return int(MLTrainingStore().record_count())
    except Exception:
        return 0


def ml_clean_start_date(cfg: Config) -> str:
    return str(
        cfg.get("stats_exclude_pre_fix_date") or cfg.get("ml_clean_start_date") or ""
    ).strip()


_ml_clean_training_rows_lock = threading.Lock()
_ml_clean_training_rows_cache: dict[str, int] = {}


def invalidate_ml_clean_training_rows_cache() -> None:
    """Force refresh after ML training store changes (position open/close)."""
    with _ml_clean_training_rows_lock:
        _ml_clean_training_rows_cache.clear()


def _ml_clean_training_rows_cache_key(cfg: Config) -> str:
    start = ml_clean_start_date(cfg)
    return start if start else "__all__"


def _query_ml_clean_training_rows(cfg: Config) -> int:
    """ML training rows on/after the post-fix clean baseline date (SQLite)."""
    try:
        from data.ml_training_store import MLTrainingStore

        store = MLTrainingStore()
        start = ml_clean_start_date(cfg)
        if start:
            return int(store.record_count_since(start))
        return int(store.record_count())
    except Exception:
        return 0


def ml_clean_training_rows(cfg: Config) -> int:
    """
    Session-scoped ML row count — one SQLite count per process until invalidated.

    Invalidated on position open/close via ``execution.ml_training_hooks``.
    """
    key = _ml_clean_training_rows_cache_key(cfg)
    with _ml_clean_training_rows_lock:
        if key in _ml_clean_training_rows_cache:
            try:
                from system.diagnostics.perf_metrics import record_ml_rows_cache

                record_ml_rows_cache(hit=True)
            except Exception:
                pass
            return _ml_clean_training_rows_cache[key]

    try:
        from system.diagnostics.perf_metrics import record_ml_rows_cache

        record_ml_rows_cache(hit=False)
    except Exception:
        pass

    count = _query_ml_clean_training_rows(cfg)
    with _ml_clean_training_rows_lock:
        _ml_clean_training_rows_cache[key] = count
    return count


def should_use_interim_scorer(cfg: Config) -> bool:
    return ml_clean_training_rows(cfg) < ml_min_rows_for_model(cfg)


def _trend_score(
    last: dict[str, Any],
    c15_last: dict[str, Any] | None,
    *,
    max_pts: float,
    direction: str,
) -> float:
    try:
        atr = max(1e-6, float(last.get("atr", 0) or 0))
        sep_5 = abs(float(last.get("fast_ema", 0)) - float(last.get("slow_ema", 0)))
        sep_15 = 0.0
        if c15_last is not None:
            sep_15 = abs(
                float(c15_last.get("fast_ema", 0)) - float(c15_last.get("slow_ema", 0))
            )
        ratio = (sep_5 + sep_15 * 0.5) / atr
        rsi = float(last.get("rsi", 50) or 50)
        aligned = False
        if direction == "BUY":
            aligned = float(last.get("fast_ema", 0)) > float(last.get("slow_ema", 0))
            rsi_ok = rsi >= 50
        elif direction == "SELL":
            aligned = float(last.get("fast_ema", 0)) < float(last.get("slow_ema", 0))
            rsi_ok = rsi <= 50
        else:
            rsi_ok = False
        if ratio >= 1.2 and aligned and rsi_ok:
            return max_pts
        if ratio >= 0.6 and aligned:
            return max_pts * 0.6
        return max_pts * 0.2
    except Exception:
        return max_pts * 0.2


_SESSION_SCORES = {
    "london_morning": 1.0,
    "london_us_overlap": 1.0,
    "us_afternoon": 0.8,
    "asia_early": 0.4,
    "late": 0.2,
}


def _session_score(at: datetime | None, *, max_pts: float) -> float:
    name = session_name(at)
    factor = _SESSION_SCORES.get(name, 0.0)
    return max_pts * factor


def _volatility_score(atr_series: pd.Series | None, *, max_pts: float) -> float:
    if atr_series is None or len(atr_series.dropna()) < 20:
        return max_pts * 0.5
    values = atr_series.dropna()
    ref = values.iloc[-min(20, len(values)) :]
    current = float(values.iloc[-1])
    pct = float(ref.rank(pct=True).iloc[-1] * 100.0)
    if 60 <= pct <= 80:
        return max_pts
    if 40 <= pct < 60:
        return max_pts * 0.6
    if pct < 40:
        return max_pts * 0.2
    return max_pts * 0.4


def _min_recent_score(cfg: Config) -> float:
    block = cfg.get("interim_scorer")
    if isinstance(block, dict) and block.get("interim_scorer_min_recent_score") is not None:
        return float(block["interim_scorer_min_recent_score"])
    raw = cfg.get("interim_scorer_min_recent_score")
    if raw is not None:
        return float(raw)
    return 20.0


def _recent_performance_score(
    store: Any | None, *, max_pts: float, cfg: Config | None = None
) -> float:
    floor = _min_recent_score(cfg) if cfg is not None else 20.0
    if store is None or not hasattr(store, "recent_confirmed_closed_trades"):
        return floor
    try:
        rows = store.recent_confirmed_closed_trades(10)
    except Exception:
        return floor
    if len(rows) < 5:
        return floor
    wins = 0
    for row in rows:
        result = str(row.get("result") or "").upper()
        if result == "WIN":
            wins += 1
        elif not result:
            pnl = row.get("ig_pnl_currency", row.get("pnl"))
            try:
                if float(pnl) > 0:
                    wins += 1
            except (TypeError, ValueError):
                pass
    wr = wins / len(rows)
    if wr > 0.5:
        return max_pts
    if wr >= 0.4:
        return max_pts * 0.6
    return max_pts * 0.2


class InterimConfidenceScorer:
    def score(
        self,
        *,
        cfg: Config,
        market: str,
        direction: str,
        snapshot: dict[str, Any],
        store: Any | None = None,
        now: datetime | None = None,
    ) -> InterimScore:
        weights = _weights(cfg)
        last = snapshot.get("last")
        if last is not None and hasattr(last, "to_dict"):
            last = last.to_dict()
        last = dict(last or {})
        trend15 = snapshot.get("trend15")
        if trend15 is not None and hasattr(trend15, "to_dict"):
            trend15 = trend15.to_dict()
        atr_series = snapshot.get("atr_series")
        trend = _trend_score(
            last, trend15, max_pts=weights["trend"], direction=direction
        )
        session = _session_score(now, max_pts=weights["session"])
        vol = _volatility_score(atr_series, max_pts=weights["volatility"])
        recent = _recent_performance_score(
            store, max_pts=weights["recent_performance"], cfg=cfg
        )
        total = max(0.0, min(100.0, trend + session + vol + recent))
        notes = (
            f"trend={trend:.0f} session={session:.0f} vol={vol:.0f} "
            f"recent={recent:.0f} total={total:.0f}"
        )
        log_engine(f"[INTERIM SCORER] {market}: {notes}")
        return InterimScore(
            total=total,
            trend=trend,
            session=session,
            volatility=vol,
            recent_performance=recent,
            notes=notes,
        )


_SCORER = InterimConfidenceScorer()


def get_interim_scorer() -> InterimConfidenceScorer:
    return _SCORER
