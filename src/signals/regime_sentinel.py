"""
MacroRegimeSentinel — async extra-market regime classification for gate threshold steering.

Continuously analyzes multi-hour rolling volatility, volume trend spreads, and liquidity
channels; emits TREND_ACCELERATED | RANGE_COMPRESSED | LIQUIDITY_EVAPORATED.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

import numpy as np

from system.engine_log import log_engine

REGIME_TREND_ACCELERATED = "TREND_ACCELERATED"
REGIME_RANGE_COMPRESSED = "RANGE_COMPRESSED"
REGIME_LIQUIDITY_EVAPORATED = "LIQUIDITY_EVAPORATED"

_POLL_SEC = 12.0
_VOL_WINDOW = 48
_VOLUME_WINDOW = 32

_lock = threading.RLock()
_sentinel: MacroRegimeSentinel | None = None


class MacroRegimeSentinel:
    """Isolated daemon thread — macro regime token for 12-gate risk validation."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._regime = REGIME_RANGE_COMPRESSED
        self._volatility_pct = 0.0
        self._volume_trend = 0.0
        self._liquidity_score = 1.0
        self._vol_history: deque[float] = deque(maxlen=_VOL_WINDOW)
        self._volume_history: deque[float] = deque(maxlen=_VOLUME_WINDOW)
        self._updated_at = 0.0

    @property
    def current_regime(self) -> str:
        with _lock:
            return str(self._regime)

    def snapshot(self) -> dict[str, Any]:
        with _lock:
            return {
                "regime": self._regime,
                "volatility_pct": round(self._volatility_pct, 4),
                "volume_trend": round(self._volume_trend, 4),
                "liquidity_score": round(self._liquidity_score, 4),
                "threshold_multiplier": self.threshold_multiplier(),
                "updated_at": self._updated_at,
            }

    def threshold_multiplier(self) -> float:
        """Expand entries in trend; contract in range / evaporated liquidity."""
        regime = self.current_regime
        if regime == REGIME_TREND_ACCELERATED:
            return 0.92
        if regime == REGIME_LIQUIDITY_EVAPORATED:
            return 1.15
        return 1.08

    def confidence_relief_points(self) -> float:
        """Points shaved from entry confidence floor when trend accelerates."""
        if self.current_regime == REGIME_TREND_ACCELERATED:
            return 6.0
        if self.current_regime == REGIME_LIQUIDITY_EVAPORATED:
            return -8.0
        return 0.0

    def start(self) -> None:
        with _lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="macro-regime-sentinel",
                daemon=True,
            )
            self._thread.start()
            log_engine("MacroRegimeSentinel: async daemon started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._analyze_once()
            except Exception as exc:
                log_engine(
                    f"MacroRegimeSentinel tick error: {type(exc).__name__}: {exc}"
                )
            if self._stop.wait(_POLL_SEC):
                break

    def _analyze_once(self) -> None:
        vol_samples: list[float] = []
        volume_samples: list[float] = []
        liquidity_hits = 0
        liquidity_total = 0

        try:
            from trading.multi_api_broker import get_multi_api_broker

            broker = get_multi_api_broker()
            for epic in broker._epics if hasattr(broker, "_epics") else ():
                macro = broker.macro_snapshot(str(epic))
                vol_samples.append(float(macro.get("volatility_pct") or 0.0))
                liquidity_total += 1
                if float(macro.get("regular_market_price") or 0.0) > 0:
                    liquidity_hits += 1
        except Exception:
            pass

        try:
            from system.market_data_hub import get_market_data_hub

            hub = get_market_data_hub()
            for epic in (
                "CS.D.CFPGOLD.CFP.IP",
                "IX.D.DOW.IFM.IP",
                "IX.D.NIKKEI.IFM.IP",
                "CS.D.EURUSD.CFD.IP",
            ):
                snap = hub.get_snapshot(epic)
                if snap is None:
                    continue
                spread = float(getattr(snap, "offer", 0) or 0) - float(
                    getattr(snap, "bid", 0) or 0
                )
                volume_samples.append(max(0.0, spread))
        except Exception:
            pass

        vol_pct = float(np.mean(vol_samples)) if vol_samples else 0.0
        vol_trend = 0.0
        if vol_samples:
            self._vol_history.append(vol_pct)
        if len(self._vol_history) >= 4:
            arr = np.asarray(list(self._vol_history), dtype=np.float64)
            vol_trend = float(arr[-1] - arr[0])

        vol_mean = float(np.mean(volume_samples)) if volume_samples else 0.0
        if volume_samples:
            self._volume_history.append(vol_mean)
        volume_trend = 0.0
        if len(self._volume_history) >= 4:
            varr = np.asarray(list(self._volume_history), dtype=np.float64)
            volume_trend = float(varr[-1] - varr[0])

        liquidity_score = (
            float(liquidity_hits) / float(liquidity_total)
            if liquidity_total > 0
            else 0.5
        )

        regime = REGIME_RANGE_COMPRESSED
        if liquidity_score < 0.35 or vol_pct < 0.05:
            regime = REGIME_LIQUIDITY_EVAPORATED
        elif vol_pct > 0.28 and vol_trend > 0.02:
            regime = REGIME_TREND_ACCELERATED
        elif vol_pct < 0.12 and abs(volume_trend) < 0.01:
            regime = REGIME_RANGE_COMPRESSED

        with _lock:
            prev = self._regime
            self._regime = regime
            self._volatility_pct = vol_pct
            self._volume_trend = volume_trend
            self._liquidity_score = liquidity_score
            self._updated_at = time.time()
            if regime != prev:
                log_engine(
                    f"MacroRegimeSentinel: {prev} → {regime} "
                    f"(vol={vol_pct:.3f} liq={liquidity_score:.2f})"
                )


def get_macro_regime_sentinel() -> MacroRegimeSentinel:
    global _sentinel
    with _lock:
        if _sentinel is None:
            _sentinel = MacroRegimeSentinel()
        return _sentinel


def start_macro_regime_sentinel() -> MacroRegimeSentinel:
    sentinel = get_macro_regime_sentinel()
    sentinel.start()
    return sentinel


def reset_macro_regime_sentinel_for_tests() -> None:
    global _sentinel
    with _lock:
        if _sentinel is not None:
            _sentinel.stop()
        _sentinel = None
