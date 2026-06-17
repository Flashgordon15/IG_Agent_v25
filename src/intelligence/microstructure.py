"""
Multi-timeframe micro-structure classifier — 5s / 1m / 5m tick synthesis.

Lightweight numpy rolling features for momentum, sweep, and order-block patterns.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from intelligence.types import MicroRegime, MicrostructureVerdict

TF_5S = 5.0
TF_1M = 60.0
TF_5M = 300.0
MAX_BUFFER_SEC = TF_5M + 30.0
MIN_WARMUP_TICKS = 12
HISTORICAL_WARMUP_BARS = 100
SYNTHETIC_TICKS_PER_BAR = 6
RSI_VELOCITY_CEILING = 85.0
VELOCITY_ENGAGED_TICKS = 15
VELOCITY_WINDOW_SEC = 0.2
FIRST_LIVE_BLEND_RATIO = 0.10


@dataclass(frozen=True)
class _Tick:
    ts: float
    mid: float
    spread: float


def _mid(bid: float, offer: float) -> float:
    return (float(bid) + float(offer)) / 2.0


def _momentum_slope(mids: np.ndarray) -> float:
    if mids.size < 3:
        return 0.0
    x = np.arange(mids.size, dtype=np.float64)
    try:
        coeffs = np.polyfit(x, mids, 1)
        if coeffs.size == 0:
            return 0.0
        return float(coeffs[0])
    except (ValueError, np.linalg.LinAlgError, FloatingPointError):
        return 0.0


def _window_ticks(ticks: list[_Tick], horizon_sec: float, now: float) -> list[_Tick]:
    cutoff = now - horizon_sec
    return [t for t in ticks if t.ts >= cutoff]


class MicrostructureClassifier:
    """
    Classify short-term price action from hub/Lightstreamer ticks.

    Regimes: momentum up/down, liquidity sweeps, consolidation order blocks.
    """

    def __init__(
        self,
        *,
        sweep_sigma: float = 2.5,
        block_range_pct: float = 0.0008,
        min_ticks_5s: int = 4,
    ) -> None:
        self._sweep_sigma = float(sweep_sigma)
        self._block_range_pct = float(block_range_pct)
        self._min_ticks_5s = max(2, int(min_ticks_5s))
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[_Tick]] = {}
        self._warmup_blend_pending: dict[str, bool] = {}
        self._live_tick_seen: dict[str, bool] = {}

    def velocity_engaged(self, epic: str, *, now: float | None = None) -> bool:
        """True when order-book tick velocity exceeds ENGAGED threshold (15 ticks / 200ms)."""
        return self.ticks_in_window(
            epic, VELOCITY_WINDOW_SEC, now=now
        ) > VELOCITY_ENGAGED_TICKS

    def effective_entry_rsi_ceiling(
        self,
        epic: str,
        *,
        base_ceiling: float = RSI_VELOCITY_CEILING,
        now: float | None = None,
    ) -> float:
        """
        Entry criteria — disable RSI <= 85 ceiling when velocity ENGAGED.

        High-velocity breakouts bypass the overbought filter (returns 99.0).
        """
        ceiling = float(base_ceiling or RSI_VELOCITY_CEILING)
        if ceiling <= 0:
            return ceiling
        if self.velocity_engaged(epic, now=now):
            return 99.0
        return ceiling

    def _normalize_live_tick_after_warmup(
        self,
        epic: str,
        bid: float,
        offer: float,
        ts: float,
    ) -> tuple[float, float, float]:
        """
        Blend first live Lightstreamer packet toward synthetic REST warmup centroid.

        Prevents false Z-score spikes when historical bar structure diverges from hub ticks.
        """
        key = str(epic or "").strip()
        with self._lock:
            pending = self._warmup_blend_pending.get(key, False)
            buf = list(self._buffers.get(key, ()))
        if not pending or len(buf) < MIN_WARMUP_TICKS:
            return bid, offer, ts
        mids = np.array([t.mid for t in buf[-MIN_WARMUP_TICKS:]], dtype=np.float64)
        spreads = np.array([t.spread for t in buf[-MIN_WARMUP_TICKS:]], dtype=np.float64)
        ref_mid = float(np.median(mids))
        ref_spread = float(np.median(spreads))
        live_mid = _mid(bid, offer)
        live_spread = max(offer - bid, 1e-12)
        blend = FIRST_LIVE_BLEND_RATIO
        norm_mid = (1.0 - blend) * ref_mid + blend * live_mid
        norm_spread = (1.0 - blend) * ref_spread + blend * max(
            live_spread, ref_spread * 0.5
        )
        with self._lock:
            self._warmup_blend_pending[key] = False
            self._live_tick_seen[key] = True
        return (
            norm_mid - norm_spread / 2.0,
            norm_mid + norm_spread / 2.0,
            ts,
        )

    def _mark_warmup_complete(self, epic: str) -> None:
        key = str(epic or "").strip()
        if not key:
            return
        with self._lock:
            self._warmup_blend_pending[key] = True
            self._live_tick_seen[key] = False

    def tick_count(self, epic: str) -> int:
        key = str(epic or "").strip()
        with self._lock:
            return len(self._buffers.get(key, ()))

    def ticks_in_window(
        self,
        epic: str,
        window_sec: float,
        *,
        now: float | None = None,
    ) -> int:
        """Count hub ticks received within the trailing *window_sec* (e.g. 0.2 for 200ms)."""
        key = str(epic or "").strip()
        if not key or window_sec <= 0:
            return 0
        ts_now = float(now or time.time())
        with self._lock:
            buf = list(self._buffers.get(key, ()))
        return len(_window_ticks(buf, float(window_sec), ts_now))

    def needs_historical_warmup(self, epic: str) -> bool:
        return self.tick_count(epic) < MIN_WARMUP_TICKS

    def clear_buffer(self, epic: str) -> None:
        key = str(epic or "").strip()
        if not key:
            return
        with self._lock:
            self._buffers.pop(key, None)

    def record_tick(
        self,
        epic: str,
        *,
        bid: float,
        offer: float,
        ts: float | None = None,
        source: str = "live",
    ) -> None:
        key = str(epic or "").strip()
        if not key or bid <= 0 or offer <= 0:
            return
        tick_ts = float(ts or time.time())
        if source == "live":
            bid, offer, tick_ts = self._normalize_live_tick_after_warmup(
                key, bid, offer, tick_ts
            )
        tick = _Tick(ts=tick_ts, mid=_mid(bid, offer), spread=offer - bid)
        with self._lock:
            buf = self._buffers.setdefault(key, deque())
            buf.append(tick)
            cutoff = tick.ts - MAX_BUFFER_SEC
            while buf and buf[0].ts < cutoff:
                buf.popleft()

    def _ingest_ticks(self, epic: str, ticks: list[_Tick]) -> int:
        key = str(epic or "").strip()
        if not key or not ticks:
            return 0
        ordered = sorted(ticks, key=lambda t: t.ts)
        with self._lock:
            buf = self._buffers.setdefault(key, deque())
            for tick in ordered:
                buf.append(tick)
            cutoff = ordered[-1].ts - MAX_BUFFER_SEC
            while buf and buf[0].ts < cutoff:
                buf.popleft()
        self._mark_warmup_complete(key)
        return len(ordered)

    def seed_from_quotes(
        self,
        epic: str,
        quotes: list[Any],
        *,
        anchor_now: float | None = None,
    ) -> int:
        """Map OHLC seed quotes into a recent synthetic tick window for cold-start warmup."""
        key = str(epic or "").strip()
        if not key or not quotes:
            return 0
        now = float(anchor_now or time.time())
        ordered = sorted(quotes, key=lambda q: getattr(q, "time", 0))
        take = ordered[-min(len(ordered), HISTORICAL_WARMUP_BARS) :]
        span = max(TF_5M, MAX_BUFFER_SEC - 5.0)
        synth: list[_Tick] = []
        n = len(take)
        for i, quote in enumerate(take):
            bid = float(getattr(quote, "bid", 0) or 0)
            offer = float(getattr(quote, "offer", 0) or 0)
            if bid <= 0 or offer <= bid:
                continue
            spread = offer - bid
            mid = _mid(bid, offer)
            frac = i / max(1, n - 1)
            base_ts = now - span + frac * span
            step = span / max(1, n * SYNTHETIC_TICKS_PER_BAR)
            for j in range(SYNTHETIC_TICKS_PER_BAR):
                synth.append(
                    _Tick(
                        ts=base_ts + j * step,
                        mid=mid,
                        spread=spread,
                    )
                )
        return self._ingest_ticks(key, synth)

    def seed_from_ohlc_bars(
        self,
        epic: str,
        bars: list[dict[str, Any]],
        *,
        anchor_now: float | None = None,
    ) -> int:
        """Expand IG REST OHLC bars into synthetic hub ticks anchored to *now*."""
        from data.models import Quote

        from trading.ohlc_bootstrap import _parse_bar_time

        quotes: list[Quote] = []
        for bar in bars:
            high = float(bar.get("high") or 0)
            low = float(bar.get("low") or 0)
            if high <= 0 or low <= 0:
                continue
            mid = (high + low) / 2.0
            bid_close = float(bar.get("bid_close") or 0)
            offer_close = float(bar.get("offer_close") or 0)
            if bid_close > 0 and offer_close > bid_close:
                bid = bid_close
                offer = offer_close
            else:
                spread = max(1.0, float(bar.get("close") or mid) * 0.0001)
                bid = mid - spread / 2.0
                offer = mid + spread / 2.0
            quotes.append(
                Quote(time=_parse_bar_time(bar.get("time", "")), bid=bid, offer=offer)
            )
        return self.seed_from_quotes(epic, quotes, anchor_now=anchor_now)

    def bootstrap_historical_warmup(
        self,
        rest_client: Any,
        epic: str,
        *,
        num_points: int = HISTORICAL_WARMUP_BARS,
        resolution: str = "MINUTE_5",
    ) -> int:
        """Pull IG REST history when local tick cache is empty after Genesis purge."""
        key = str(epic or "").strip()
        if not key or not self.needs_historical_warmup(key):
            return self.tick_count(key)
        fetch = getattr(rest_client, "fetch_price_history", None)
        if not callable(fetch):
            return 0
        try:
            from system.rest_api_budget import ohlc_bootstrap_rest_window

            with ohlc_bootstrap_rest_window():
                bars = fetch(key, resolution=resolution, num_points=num_points)
        except Exception:
            bars = []
        if not bars:
            return self.tick_count(key)
        from system.engine_log import log_engine

        count = self.seed_from_ohlc_bars(key, bars)
        log_engine(
            f"Microstructure warmup: seeded {count} synthetic ticks for {key} "
            f"from {len(bars)} REST bars"
        )
        return count

    def _features(self, ticks: list[_Tick], horizon: float, now: float) -> dict[str, float]:
        window = _window_ticks(ticks, horizon, now)
        if len(window) < 2:
            return {"momentum": 0.0, "volatility": 0.0, "range_pct": 0.0, "sweep": 0.0}
        mids = np.array([t.mid for t in window], dtype=np.float64)
        rets = np.diff(mids) / np.maximum(mids[:-1], 1e-12)
        vol = float(np.std(rets)) if rets.size else 0.0
        mom = _momentum_slope(mids)
        hi, lo = float(np.max(mids)), float(np.min(mids))
        mid_ref = float(mids[-1]) or 1.0
        range_pct = (hi - lo) / abs(mid_ref)
        sweep = 0.0
        if rets.size and vol > 1e-12:
            sweep = float(np.max(np.abs(rets))) / vol
        return {
            "momentum": mom,
            "volatility": vol,
            "range_pct": range_pct,
            "sweep": sweep,
        }

    def classify(self, epic: str, *, now: float | None = None) -> MicrostructureVerdict:
        key = str(epic or "").strip()
        if not key:
            from intelligence.defensive_defaults import neutral_microstructure_verdict

            return neutral_microstructure_verdict("", reason="missing_epic")
        ts_now = float(now or time.time())
        try:
            with self._lock:
                buf = list(self._buffers.get(key, ()))
        except Exception:
            from intelligence.defensive_defaults import neutral_microstructure_verdict

            return neutral_microstructure_verdict(key, reason="buffer_unavailable")

        try:
            f5 = self._features(buf, TF_5S, ts_now)
            f1 = self._features(buf, TF_1M, ts_now)
            f5m = self._features(buf, TF_5M, ts_now)
        except (ValueError, FloatingPointError, IndexError):
            from intelligence.defensive_defaults import neutral_microstructure_verdict

            return neutral_microstructure_verdict(key, reason="feature_compute_failed")

        regime: MicroRegime = "NEUTRAL"
        confidence = 0.35
        sweep = False
        order_block = False
        detail_parts: list[str] = []

        w5 = _window_ticks(buf, TF_5S, ts_now)

        if len(w5) >= self._min_ticks_5s:
            if f5["sweep"] >= self._sweep_sigma:
                sweep = True
                regime = "SWEEP_BUY" if f5["momentum"] > 0 else "SWEEP_SELL"
                confidence = min(0.95, 0.55 + 0.1 * f5["sweep"])
                detail_parts.append(f"sweep σ={f5['sweep']:.2f}")
            elif f1["range_pct"] <= self._block_range_pct and abs(f5["momentum"]) > abs(
                f1["momentum"]
            ) * 3:
                order_block = True
                regime = "ORDER_BLOCK"
                confidence = 0.62
                detail_parts.append("compression breakout")
            elif f5["momentum"] > 0 and f1["momentum"] > 0:
                regime = "MOMENTUM_UP"
                confidence = min(0.9, 0.45 + abs(f5["momentum"]) * 1e4)
                detail_parts.append("aligned up momentum")
            elif f5["momentum"] < 0 and f1["momentum"] < 0:
                regime = "MOMENTUM_DOWN"
                confidence = min(0.9, 0.45 + abs(f5["momentum"]) * 1e4)
                detail_parts.append("aligned down momentum")

        if len(buf) >= 2 and regime == "NEUTRAL" and confidence == 0.35:
            vol_boost = min(0.25, f1["volatility"] * 120.0 + f5m["range_pct"] * 40.0)
            mom_boost = min(0.2, abs(f1["momentum"]) * 5e3 + abs(f5m["momentum"]) * 2e3)
            confidence = min(0.85, 0.38 + vol_boost + mom_boost)
            if vol_boost + mom_boost > 0.01:
                detail_parts.append("live tick synthesis")

        try:
            from intelligence.liquidity_wave import apply_microstructure_wave

            confidence, wave_note = apply_microstructure_wave(
                confidence,
                regime,
                now=datetime.fromtimestamp(ts_now, tz=ZoneInfo("Europe/London")),
            )
            detail_parts.append(wave_note)
        except Exception:
            pass

        try:
            from intelligence.macro_radar import macro_confidence_adjustment

            confidence = macro_confidence_adjustment(key, confidence, regime)
            detail_parts.append("macro_radar")
        except Exception:
            pass

        velocity_note = ""
        if self.velocity_engaged(key, now=ts_now):
            velocity_note = "velocity_engaged_rsi_override"
            detail_parts.append(velocity_note)

        return MicrostructureVerdict(
            epic=key,
            regime=regime,
            confidence=confidence,
            momentum_5s=f5["momentum"],
            momentum_1m=f1["momentum"],
            momentum_5m=f5m["momentum"],
            sweep_detected=sweep,
            order_block_detected=order_block,
            detail="; ".join(detail_parts) if detail_parts else "neutral microstructure",
        )

    def classify_safe(self, epic: str, *, now: float | None = None) -> MicrostructureVerdict:
        try:
            return self.classify(epic, now=now)
        except Exception:
            from intelligence.defensive_defaults import neutral_microstructure_verdict

            return neutral_microstructure_verdict(str(epic or ""), reason="classify_error")

    def snapshot(self, epic: str) -> dict[str, Any]:
        v = self.classify(epic)
        return {
            "epic": v.epic,
            "regime": v.regime,
            "confidence": v.confidence,
            "momentum_5s": v.momentum_5s,
            "momentum_1m": v.momentum_1m,
            "momentum_5m": v.momentum_5m,
            "sweep_detected": v.sweep_detected,
            "order_block_detected": v.order_block_detected,
            "detail": v.detail,
        }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._buffers.clear()
            self._warmup_blend_pending.clear()
            self._live_tick_seen.clear()


def effective_entry_rsi_ceiling(
    epic: str,
    *,
    base_ceiling: float = RSI_VELOCITY_CEILING,
) -> float:
    """Module-level helper for SignalEngine entry criteria."""
    try:
        from intelligence.intelligence_worker import get_intelligence_worker

        clf = get_intelligence_worker().micro_model
        return clf.effective_entry_rsi_ceiling(epic, base_ceiling=base_ceiling)
    except Exception:
        return float(base_ceiling or RSI_VELOCITY_CEILING)


def velocity_engaged(epic: str) -> bool:
    try:
        from intelligence.intelligence_worker import get_intelligence_worker

        return get_intelligence_worker().micro_model.velocity_engaged(epic)
    except Exception:
        return False


def bootstrap_microstructure_for_loop(
    loop: Any,
    rest_client: Any | None,
    *,
    classifier: MicrostructureClassifier | None = None,
) -> int:
    """Warm microstructure buffers from SignalEngine OHLC seed or IG REST on boot."""
    epic = str(getattr(loop, "_epic", "") or "").strip()
    if not epic:
        return 0
    clf = classifier
    if clf is None:
        from intelligence.intelligence_worker import get_intelligence_worker

        clf = get_intelligence_worker().micro_model
    if not clf.needs_historical_warmup(epic):
        return clf.tick_count(epic)

    market = str(getattr(loop, "_market", "") or epic)
    signal_engine = getattr(loop, "_signal_engine", None)
    if signal_engine is not None:
        try:
            df = signal_engine.quote_df(market)
            if not df.empty:
                from data.models import Quote

                quotes = [
                    Quote(
                        time=row.time.to_pydatetime()
                        if hasattr(row.time, "to_pydatetime")
                        else row.time,
                        bid=float(row.bid),
                        offer=float(row.offer),
                    )
                    for row in df.itertuples()
                ]
                seeded = clf.seed_from_quotes(epic, quotes)
                if seeded >= MIN_WARMUP_TICKS:
                    return seeded
        except Exception:
            pass

    if rest_client is not None:
        return clf.bootstrap_historical_warmup(rest_client, epic)
    return clf.tick_count(epic)


def bootstrap_microstructure_parallel(
    rest_client: Any,
    loops: list[Any],
    *,
    on_loop_complete: Any | None = None,
) -> None:
    """Post-OHLC Gate 4 hook — pre-populate microstructure tick buffers before stream."""
    if not loops:
        return
    from intelligence.intelligence_worker import get_intelligence_worker

    worker = get_intelligence_worker()
    clf = worker.micro_model
    for loop in loops:
        try:
            count = bootstrap_microstructure_for_loop(
                loop, rest_client, classifier=clf
            )
            worker.publish_microstructure_verdict(
                str(getattr(loop, "_epic", "") or ""),
                clf.classify(str(getattr(loop, "_epic", "") or "")),
            )
            from system.engine_log import log_engine

            log_engine(
                f"Gate4 microstructure warmup: {getattr(loop, '_epic', '?')} "
                f"({count} ticks)"
            )
            if on_loop_complete is not None:
                on_loop_complete(loop)
        except Exception as exc:
            from system.engine_log import log_engine

            log_engine(
                f"Gate4 microstructure warmup skipped "
                f"{getattr(loop, '_epic', '?')}: {type(exc).__name__}: {exc}"
            )
