"""
Quantum-Multi-Market (QMM) dynamic asset selector — in-memory pre-gate scan envelope.

Ranks epics from live hub metrics (ATR delta, spread cost, chart alignment, sentiment)
without disk I/O. Indicator math is read from existing EnvironmentScorer outputs only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

_QMM_RANK_INTERVAL_SEC = 60.0
_RANK_FLOOR = 0.01

_last_rank_mono: float = 0.0
_cached_ranked: list[tuple[str, float]] = []
_cache_lock = threading.Lock()


@dataclass
class QmmEpicMetrics:
    epic: str
    atr_delta: float = 0.0
    trend_score: float = _RANK_FLOOR
    pattern_score: float = _RANK_FLOOR
    sentiment_score: float = 0.5
    spread_cost: float = 1.0
    rank_score: float = _RANK_FLOOR
    detail: dict[str, Any] = field(default_factory=dict)


def _hub_atr_delta(epic: str, loop: Any) -> float:
    """Relative ATR elevation vs configured stop baseline (dimensionless, >= 0)."""
    current_atr = 0.0
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is not None:
            current_atr = float(getattr(snap, "atr", 0) or 0)
    except Exception:
        pass
    if current_atr <= 0:
        env = getattr(loop, "_env", None)
        if env is not None and hasattr(env, "get_factors"):
            try:
                current_atr = float((env.get_factors() or {}).get("atr") or 0)
            except Exception:
                current_atr = 0.0
    if current_atr <= 0:
        return 0.0
    cfg = getattr(loop, "_config", None)
    stop = float(getattr(cfg, "stop_distance_points", 40) or 40)
    baseline = max(stop * 0.5, 1.0)
    return max(0.0, (current_atr - baseline) / baseline)


def _sentiment_score(env: Any, market: str) -> float:
    """Map IG sentiment label to 0..1 (neutral=0.5)."""
    try:
        if env is None or not hasattr(env, "get_sentiment_factor"):
            return 0.5
        sent = env.get_sentiment_factor(market) or {}
        label = str(sent.get("label") or "neutral").lower()
        if label in ("bullish", "long", "buy"):
            return 0.75
        if label in ("bearish", "short", "sell"):
            return 0.25
        if label in ("crowded_long", "crowded_short"):
            return 0.35
        return 0.5
    except Exception:
        return 0.5


def _pattern_proxy_score(loop: Any) -> float:
    """
    Geometric chart pattern proxy from existing 15m indicator row (no new indicator math).
    Uses EMA gap / ATR as consolidation-vs-breakout geometry.
    """
    from runtime.market_orchestrator import compute_rotation_trend_cleanliness

    engine = getattr(loop, "_signal_engine", None)
    market = str(getattr(loop, "_market", "") or "")
    if engine is None or not market:
        return _RANK_FLOOR
    try:
        df = engine.quote_df(market)
        c15 = engine.candles(df, 15)
        if len(c15) < 2:
            return _RANK_FLOOR
        c15i = engine.add_indicators(c15)
        row15 = c15i.iloc[-2]
        atr_15 = float(row15.get("atr", 0) or 0)
        return compute_rotation_trend_cleanliness(row15, atr_15m=atr_15)
    except Exception:
        return _RANK_FLOOR


def extract_qmm_epic_metrics(
    epic: str,
    loop: Any,
    *,
    trend_cleanliness: float,
    relative_spread_cost: float,
) -> QmmEpicMetrics:
    env = getattr(loop, "_env", None)
    market = str(getattr(loop, "_market", "") or "")
    atr_delta = _hub_atr_delta(epic, loop)
    pattern = _pattern_proxy_score(loop)
    sentiment = _sentiment_score(env, market)
    spread_cost = max(float(relative_spread_cost or 1.0), _RANK_FLOOR)
    trend = max(float(trend_cleanliness or _RANK_FLOOR), _RANK_FLOOR)

    # Combined QMM rank: movement quality / spread, boosted by vol delta + sentiment conviction.
    vol_boost = 1.0 + min(0.5, atr_delta)
    sent_boost = 0.85 + 0.3 * sentiment
    pattern_boost = 0.75 + 0.25 * min(1.0, pattern / 30.0)
    rank = (trend * pattern_boost * vol_boost * sent_boost) / spread_cost
    rank = max(rank, _RANK_FLOOR)

    return QmmEpicMetrics(
        epic=epic,
        atr_delta=round(atr_delta, 4),
        trend_score=round(trend, 3),
        pattern_score=round(pattern, 3),
        sentiment_score=round(sentiment, 3),
        spread_cost=round(spread_cost, 3),
        rank_score=round(rank, 4),
        detail={
            "vol_boost": round(vol_boost, 3),
            "sent_boost": round(sent_boost, 3),
            "pattern_boost": round(pattern_boost, 3),
        },
    )


def rank_qmm_epics(
    candidates: list[tuple[str, Any, float, float]],
    *,
    force_refresh: bool = False,
) -> list[tuple[str, float]]:
    """
    Rank (epic, loop, trend_cleanliness, relative_spread_cost) tuples.
    Cached 60s to match orchestrator rotation cadence.
    """
    global _last_rank_mono, _cached_ranked
    now = time.monotonic()
    with _cache_lock:
        if (
            not force_refresh
            and _cached_ranked
            and (now - _last_rank_mono) < _QMM_RANK_INTERVAL_SEC
        ):
            return list(_cached_ranked)

    ranked: list[tuple[str, float]] = []
    for epic, loop, trend, spread_cost in candidates:
        metrics = extract_qmm_epic_metrics(
            epic, loop, trend_cleanliness=trend, relative_spread_cost=spread_cost
        )
        ranked.append((epic, metrics.rank_score))

    ranked.sort(key=lambda item: item[1], reverse=True)
    with _cache_lock:
        _cached_ranked = list(ranked)
        _last_rank_mono = now
    return list(ranked)


def reset_qmm_asset_selector_for_tests() -> None:
    global _last_rank_mono, _cached_ranked
    with _cache_lock:
        _last_rank_mono = 0.0
        _cached_ranked = []
