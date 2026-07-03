"""128-dimensional feature state compiler — synchronized technical + ledger snapshot."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

FEATURE_STATE_DIM = 128
_FLOAT64 = np.float64


def _norm(value: Any, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if hi <= lo:
        return 0.0
    return float(max(0.0, min(1.0, (v - lo) / (hi - lo))))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def compile_current_feature_state(
    *,
    market: str = "",
    epic: str = "",
    snapshot: dict[str, Any] | None = None,
    points_ledger: dict[str, Any] | None = None,
    quote: Any = None,
) -> dict[str, Any]:
    """
    Capture synchronized 128-dim float64 state when a setup crosses ingestion threshold.

    Dimensions include RSI, EMA slopes, ATR liquidity, spread, multi-timeframe trend,
    session points ledger, and rolling score context.
    """
    snap = dict(snapshot or {})
    last = dict(snap.get("last") or {})
    trend15 = dict(snap.get("trend15") or {})
    trend60 = dict(snap.get("trend60") or {}) if snap.get("trend60") else {}
    ledger = dict(points_ledger or {})

    vec = np.zeros(FEATURE_STATE_DIM, dtype=_FLOAT64)
    close = max(_safe_float(last.get("close") or last.get("price"), 1.0), 1e-9)

    # 0–15: primary indicator block
    vec[0] = _norm(last.get("rsi", 50.0), 0.0, 100.0)
    vec[1] = _norm(last.get("atr", 0.0), 0.0, max(_safe_float(last.get("atr"), 1.0) * 4, 1.0))
    vec[2] = _norm(last.get("spread", 0.0), 0.0, 20.0)
    fast = _safe_float(last.get("fast_ema"))
    slow = _safe_float(last.get("slow_ema"))
    vec[3] = np.tanh((fast - slow) / close)
    vec[4] = 1.0 if fast > slow else 0.0
    vec[5] = _norm(snap.get("buy_score", 0.0), 0.0, 100.0)
    vec[6] = _norm(snap.get("sell_score", 0.0), 0.0, 100.0)
    vec[7] = _norm(snap.get("adjusted_confidence", 0.0), 0.0, 100.0)
    vec[8] = _norm(snap.get("raw_confidence", 0.0), 0.0, 100.0)
    vec[9] = _norm(snap.get("learning_delta", 0.0), -15.0, 15.0)
    vec[10] = 1.0 if str(snap.get("raw_signal") or "").upper() == "BUY" else 0.0
    vec[11] = 1.0 if str(snap.get("raw_signal") or "").upper() == "SELL" else 0.0
    vec[12] = 1.0 if bool(snap.get("h1_bullish")) else 0.0
    vec[13] = 1.0 if bool(snap.get("h1_bearish")) else 0.0
    vec[14] = _norm(snap.get("h1_penalty", 0.0), 0.0, 15.0)
    vec[15] = _norm(last.get("volume", 0.0), 0.0, 1e6)

    # 16–31: multi-timeframe EMA / RSI context
    vec[16] = np.tanh(
        (_safe_float(trend15.get("fast_ema")) - _safe_float(trend15.get("slow_ema"))) / close
    )
    vec[17] = _norm(trend15.get("rsi", 50.0), 0.0, 100.0)
    if trend60:
        vec[18] = np.tanh(
            (_safe_float(trend60.get("fast_ema")) - _safe_float(trend60.get("slow_ema")))
            / close
        )
        vec[19] = _norm(trend60.get("rsi", 50.0), 0.0, 100.0)
    vec[20] = _norm(last.get("open", close), close * 0.99, close * 1.01)
    vec[21] = _norm(last.get("high", close), close * 0.99, close * 1.01)
    vec[22] = _norm(last.get("low", close), close * 0.99, close * 1.01)
    vec[23] = 1.0 if _safe_float(last.get("close")) >= _safe_float(last.get("open")) else 0.0

    # 32–47: momentum / liquidity derivatives
    regime = str(snap.get("vol_regime") or "unknown").lower()
    vec[32] = 1.0 if regime == "low" else 0.0
    vec[33] = 1.0 if regime == "normal" else 0.0
    vec[34] = 1.0 if regime == "high" else 0.0
    vec[35] = _norm(snap.get("regime_penalty", 0.0), 0.0, 20.0)
    if quote is not None:
        bid = _safe_float(getattr(quote, "bid", None))
        offer = _safe_float(getattr(quote, "offer", None))
        if bid > 0 and offer > 0:
            vec[36] = _norm(offer - bid, 0.0, 20.0)

    # 48–63: rolling session points ledger
    vec[48] = _norm(ledger.get("last_trade", 0.0), -30.0, 30.0)
    vec[49] = _norm(ledger.get("session", 0.0), -30.0, 30.0)
    vec[50] = _norm(ledger.get("cumulative", 0.0), -30.0, 30.0)
    state = str(ledger.get("state") or "").upper()
    vec[51] = 1.0 if state == "HEALTHY" else 0.0
    vec[52] = 1.0 if state == "CAUTION" else 0.0
    vec[53] = 1.0 if state in ("WARNING", "DANGER", "STOP") else 0.0
    vec[54] = _norm(ledger.get("confidence_floor", 80.0), 40.0, 100.0)

    # 64–79: setup / market identity hashes (deterministic, bounded)
    for i, ch in enumerate(str(market or epic or "")[:16]):
        vec[64 + i] = (ord(ch) % 256) / 255.0

    # 80–95: timestamp cyclical encodings (session phase)
    ts_ms = time.time() * 1000.0
    hour_frac = (time.gmtime().tm_hour + time.gmtime().tm_min / 60.0) / 24.0
    vec[80] = float(np.sin(2 * np.pi * hour_frac))
    vec[81] = float(np.cos(2 * np.pi * hour_frac))
    vec[82] = _norm(ts_ms % 86400000.0, 0.0, 86400000.0)

    # 96–111: score ratios + macro/sentiment/news steering surface
    vec[96] = vec[5] - vec[6]  # directional score delta
    vec[97] = vec[7] - vec[8]  # learning-adjusted lift

    epic_key = str(epic or market or "")
    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        sfeats = sentiment_momentum_features(epic_key)
        vec[98] = _norm(sfeats.get("long_pct", 50.0), 0.0, 100.0)
        vec[99] = float(np.tanh(float(sfeats.get("delta_5m", 0.0)) * 600.0))
        vec[100] = float(np.tanh(float(sfeats.get("delta_30m", 0.0)) * 1200.0))
        vec[101] = _norm(sfeats.get("contrarian_pressure", 0.0), 0.0, 1.0)
    except Exception:
        pass
    try:
        from intelligence.macro_radar import macro_snapshot

        macro = macro_snapshot()
        vec[102] = float(np.tanh(float(macro.dxy_momentum) * 40.0))
        vec[103] = float(np.tanh(float(macro.us10y_delta) * 30.0))
        vec[104] = _norm(macro.cross_correlation, -1.0, 1.0)
    except Exception:
        pass
    try:
        from system.calendar_gate import news_proximity_features, quantize_news_countdown_vector
        from system.market_data_hub import get_news_velocity_feature_slots

        news = news_proximity_features(epic_key)
        hub_slots = get_news_velocity_feature_slots(epic_key)
        if hub_slots and len(hub_slots) >= 7:
            for i, val in enumerate(hub_slots[:7]):
                vec[105 + i] = float(val)
        else:
            vec[105] = float(news.get("countdown_norm") or 0.0)
            vec[106] = float(news.get("news_velocity") or 0.0)
            vec[107] = float(news.get("in_block_window") or 0.0)
            qnews = quantize_news_countdown_vector(epic_key, dims=4)
            for i, val in enumerate(qnews[:4]):
                vec[108 + i] = float(val)
    except Exception:
        pass

    matrix = vec.reshape(1, FEATURE_STATE_DIM).astype(_FLOAT64, copy=False)
    return {
        "matrix": matrix,
        "vector": vec,
        "ts_ms": ts_ms,
        "dim": FEATURE_STATE_DIM,
        "market": str(market or ""),
        "epic": str(epic or ""),
    }
