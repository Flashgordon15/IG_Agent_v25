"""
Interim rules-based confidence scorer — active until ML store reaches threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


def ml_clean_training_rows(cfg: Config) -> int:
    """ML training rows on/after the post-fix clean baseline date."""
    try:
        from data.ml_training_store import MLTrainingStore

        store = MLTrainingStore()
        start = ml_clean_start_date(cfg)
        if start:
            return int(store.record_count_since(start))
        return int(store.record_count())
    except Exception:
        return 0


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


def _recent_performance_score(store: Any | None, *, max_pts: float) -> float:
    if store is None or not hasattr(store, "recent_confirmed_closed_trades"):
        return max_pts * 0.48
    try:
        rows = store.recent_confirmed_closed_trades(10)
    except Exception:
        return max_pts * 0.48
    if len(rows) < 5:
        return max_pts * 0.48
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
        recent = _recent_performance_score(store, max_pts=weights["recent_performance"])
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
