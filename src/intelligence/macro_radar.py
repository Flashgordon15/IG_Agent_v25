"""
Cross-asset macro correlation radar — DXY + US 10Y lead indicators.

Non-blocking background collector feeds dynamic feature weights into
microstructure classifiers for currency and commodity front-running.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from system.engine_log import log_engine

_COLLECT_INTERVAL_SEC = 30.0
_MACRO_LOCK = threading.Lock()
_collector_thread: threading.Thread | None = None
_collector_stop = threading.Event()

# Feature weight vector: [dxy_momentum, dxy_vol, us10y_delta, us10y_vol, cross_lead]
_DEFAULT_WEIGHTS = (0.22, 0.12, 0.28, 0.14, 0.24)


@dataclass
class MacroSnapshot:
    dxy_level: float = 0.0
    dxy_momentum: float = 0.0
    dxy_volatility: float = 0.0
    us10y_yield: float = 0.0
    us10y_delta: float = 0.0
    us10y_volatility: float = 0.0
    cross_correlation: float = 0.0
    feature_weights: tuple[float, ...] = field(default_factory=lambda: _DEFAULT_WEIGHTS)
    sentiment_long_pct: float = 50.0
    sentiment_delta_5m: float = 0.0
    sentiment_delta_30m: float = 0.0
    sentiment_contrarian: float = 0.0
    news_countdown_norm: float = 0.0
    updated_at: float = 0.0
    source: str = "idle"


_snapshot = MacroSnapshot()


def _proxy_dxy_from_fx() -> tuple[float, float, float]:
    """Derive DXY proxy from EUR/USD inverse momentum when direct feed unavailable."""
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        q = hub.get_snapshot("CS.D.EURUSD.CFD.IP") if hub else None
        if q is None or q.bid <= 0:
            return 0.0, 0.0, 0.0
        mid = (q.bid + q.offer) / 2.0
        dxy_proxy = 100.0 / max(mid, 1e-6)
        return dxy_proxy, 0.0, 0.0
    except Exception:
        return 0.0, 0.0, 0.0


def _proxy_us10y_from_indices() -> tuple[float, float, float]:
    """Proxy 10Y yield delta from Wall Street risk-on impulse when bond feed absent."""
    try:
        from intelligence.intelligence_worker import get_intelligence_worker

        worker = get_intelligence_worker()
        epic = "IX.D.DOW.IFM.IP"
        v = worker.micro_model.classify(epic)
        yield_proxy = 4.0 + float(v.momentum_5m) * 1e5
        delta = float(v.momentum_5s) * 1e4
        vol = abs(float(v.momentum_1m)) * 1e4
        return max(0.5, yield_proxy), delta, vol
    except Exception:
        return 0.0, 0.0, 0.0


def _compute_feature_weights(
    dxy_mom: float,
    dxy_vol: float,
    us10y_delta: float,
    us10y_vol: float,
    cross_corr: float,
) -> tuple[float, ...]:
    """Dynamic weight array — macro lead indicators scale microstructure confidence."""
    lead_strength = min(1.0, abs(dxy_mom) * 8.0 + abs(us10y_delta) * 6.0)
    vol_damp = max(0.5, 1.0 - min(0.4, dxy_vol + us10y_vol))
    cross_boost = min(0.35, abs(cross_corr) * 0.35)
    base = list(_DEFAULT_WEIGHTS)
    scale = vol_damp * (0.75 + 0.25 * lead_strength)
    weights = tuple(round(w * scale, 4) for w in base)
    # Re-normalize with cross-correlation lead slot boosted
    total = sum(weights) or 1.0
    boosted = list(w / total for w in weights)
    boosted[4] = min(0.45, boosted[4] + cross_boost)
    norm = sum(boosted) or 1.0
    return tuple(round(w / norm, 4) for w in boosted)


def collect_macro_snapshot() -> MacroSnapshot:
    """Single non-blocking macro collection pass."""
    global _snapshot
    dxy_level, dxy_mom, dxy_vol = _proxy_dxy_from_fx()
    us10y, us10y_delta, us10y_vol = _proxy_us10y_from_indices()
    cross = 0.0
    if dxy_mom != 0.0 and us10y_delta != 0.0:
        cross = max(-1.0, min(1.0, -dxy_mom * us10y_delta * 40.0))
    weights = _compute_feature_weights(dxy_mom, dxy_vol, us10y_delta, us10y_vol, cross)
    sent_long = 50.0
    sent_d5 = 0.0
    sent_d30 = 0.0
    sent_contra = 0.0
    news_norm = 0.0
    try:
        from trading.sentiment_momentum import sentiment_momentum_features

        sfeats = sentiment_momentum_features("CS.D.EURUSD.CFD.IP")
        sent_long = float(sfeats.get("long_pct") or 50.0)
        sent_d5 = float(sfeats.get("delta_5m") or 0.0)
        sent_d30 = float(sfeats.get("delta_30m") or 0.0)
        sent_contra = float(sfeats.get("contrarian_pressure") or 0.0)
    except Exception:
        pass
    try:
        from system.calendar_gate import news_proximity_features

        nfeats = news_proximity_features("CS.D.EURUSD.CFD.IP")
        news_norm = float(nfeats.get("countdown_norm") or 0.0)
    except Exception:
        pass
    snap = MacroSnapshot(
        dxy_level=round(dxy_level, 4),
        dxy_momentum=round(dxy_mom, 6),
        dxy_volatility=round(dxy_vol, 6),
        us10y_yield=round(us10y, 4),
        us10y_delta=round(us10y_delta, 6),
        us10y_volatility=round(us10y_vol, 6),
        cross_correlation=round(cross, 4),
        feature_weights=weights,
        sentiment_long_pct=round(sent_long, 2),
        sentiment_delta_5m=round(sent_d5, 6),
        sentiment_delta_30m=round(sent_d30, 6),
        sentiment_contrarian=round(sent_contra, 4),
        news_countdown_norm=round(news_norm, 4),
        updated_at=time.time(),
        source="macro_radar",
    )
    with _MACRO_LOCK:
        _snapshot = snap
    return snap


def macro_snapshot() -> MacroSnapshot:
    with _MACRO_LOCK:
        return _snapshot


def macro_feature_weights() -> tuple[float, ...]:
    with _MACRO_LOCK:
        return _snapshot.feature_weights


def macro_confidence_adjustment(
    epic: str,
    confidence: float,
    regime: str,
) -> float:
    """Apply macro lead-indicator weights to microstructure confidence."""
    snap = macro_snapshot()
    weights = snap.feature_weights
    if not weights or snap.updated_at <= 0:
        return confidence

    epic_u = str(epic or "").upper()
    is_fx = "EUR" in epic_u or "GBP" in epic_u or "USD" in epic_u
    is_commodity = "GOLD" in epic_u or "CFP" in epic_u
    lead = (
        weights[0] * snap.dxy_momentum
        + weights[2] * snap.us10y_delta
        + weights[4] * snap.cross_correlation
    )
    if is_fx:
        lead *= 1.25
    elif is_commodity:
        lead *= 1.15

    regime_u = str(regime or "").upper()
    aligned = (
        (regime_u in ("MOMENTUM_UP", "SWEEP_BUY") and lead > 0)
        or (regime_u in ("MOMENTUM_DOWN", "SWEEP_SELL") and lead < 0)
    )
    boost = abs(lead) * 0.18 if aligned else abs(lead) * 0.06
    return max(0.0, min(0.98, confidence + boost))


def macro_radar_telemetry() -> dict[str, Any]:
    snap = macro_snapshot()
    return {
        "dxy_level": snap.dxy_level,
        "dxy_momentum": snap.dxy_momentum,
        "us10y_yield": snap.us10y_yield,
        "us10y_delta": snap.us10y_delta,
        "cross_correlation": snap.cross_correlation,
        "feature_weights": list(snap.feature_weights),
        "sentiment_long_pct": snap.sentiment_long_pct,
        "sentiment_delta_5m": snap.sentiment_delta_5m,
        "sentiment_delta_30m": snap.sentiment_delta_30m,
        "sentiment_contrarian": snap.sentiment_contrarian,
        "news_countdown_norm": snap.news_countdown_norm,
        "updated_at": snap.updated_at,
        "source": snap.source,
    }


def _collector_loop(interval_sec: float) -> None:
    from system.thread_affinity import pin_current_thread

    pin_current_thread(role="macro_radar")
    while not _collector_stop.is_set():
        try:
            collect_macro_snapshot()
        except Exception as exc:
            log_engine(f"macro_radar: collect error: {type(exc).__name__}: {exc}")
        _collector_stop.wait(interval_sec)


def start_macro_radar(*, interval_sec: float = _COLLECT_INTERVAL_SEC) -> None:
    global _collector_thread
    if _collector_thread is not None and _collector_thread.is_alive():
        return
    _collector_stop.clear()
    from system.thread_affinity import spawn_priority_thread

    _collector_thread = spawn_priority_thread(
        lambda: _collector_loop(interval_sec),
        name="MacroRadarCollector",
        role="macro_radar",
        daemon=True,
    )


def stop_macro_radar() -> None:
    _collector_stop.set()


def reset_macro_radar_for_tests() -> None:
    global _snapshot, _collector_thread
    stop_macro_radar()
    _collector_thread = None
    with _MACRO_LOCK:
        _snapshot = MacroSnapshot()
