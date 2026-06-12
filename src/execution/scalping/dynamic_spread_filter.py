"""Rolling spread MA filter — sit out toxic spread spikes."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from execution.execution_protect import protect_settings
from system.engine_log import log_engine

_filter_lock = threading.Lock()
_filter: DynamicSpreadFilter | None = None


class DynamicSpreadFilter:
    def __init__(
        self,
        *,
        periods: int = 20,
        multiplier: float = 1.5,
        min_samples: int = 5,
    ) -> None:
        self._periods = max(2, int(periods))
        self._multiplier = float(multiplier)
        self._min_samples = max(1, int(min_samples))
        self._buffers: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, epic: str, spread: float) -> None:
        if spread <= 0:
            return
        key = str(epic or "").strip()
        if not key:
            return
        with self._lock:
            buf = self._buffers.setdefault(key, deque(maxlen=self._periods))
            buf.append(float(spread))

    def moving_average(self, epic: str) -> float | None:
        key = str(epic or "").strip()
        with self._lock:
            buf = self._buffers.get(key)
            if not buf:
                return None
            return sum(buf) / len(buf)

    def allows(self, epic: str, spread: float) -> tuple[bool, str]:
        self.record(epic, spread)
        ma = self.moving_average(epic)
        if ma is None:
            return True, ""
        with self._lock:
            n = len(self._buffers.get(str(epic or "").strip(), ()))
        if n < self._min_samples:
            return True, ""
        cap = ma * self._multiplier
        if spread < cap:
            return True, ""
        msg = (
            f"Spread filter: spread {spread:.2f} >= {self._multiplier:.1f}x "
            f"MA({self._periods})={ma:.2f} (cap {cap:.2f})"
        )
        log_engine(f"EXEC_PROTECT {msg}")
        return False, msg

    def snapshot(self, epic: str) -> dict[str, Any]:
        ma = self.moving_average(epic)
        with self._lock:
            buf = self._buffers.get(str(epic or "").strip())
            samples = list(buf) if buf else []
        return {
            "periods": self._periods,
            "multiplier": self._multiplier,
            "min_samples": self._min_samples,
            "samples": len(samples),
            "moving_average": ma,
            "recent": samples[-5:] if samples else [],
        }


def get_spread_filter() -> DynamicSpreadFilter:
    global _filter
    with _filter_lock:
        if _filter is None:
            s = protect_settings()
            _filter = DynamicSpreadFilter(
                periods=int(s.get("spread_ma_periods", 20)),
                multiplier=float(s.get("spread_ma_multiplier", 1.5)),
                min_samples=int(s.get("spread_min_samples", 5)),
            )
        return _filter


def reset_spread_filter_for_tests() -> None:
    global _filter
    with _filter_lock:
        _filter = None
