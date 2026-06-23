"""
Live tick integrity filter — NaN/inf/stale/corruption guard before ML inference.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from data.models import Quote

MAX_TICK_LATENCY_MS = 250.0


@dataclass
class TickIntegrityState:
    last_valid_features: dict[str, float] = field(default_factory=dict)
    last_valid_shape: tuple[int, ...] = (0,)
    dropped_ticks: int = 0
    accepted_ticks: int = 0


class TickIntegrityFilter:
    """Stateful tick validator with last-good feature fallback."""

    def __init__(self) -> None:
        self._state = TickIntegrityState()

    @property
    def state(self) -> TickIntegrityState:
        return self._state

    def validate_quote(self, quote: Quote) -> tuple[bool, str]:
        bid = float(quote.bid)
        offer = float(quote.offer)
        if not math.isfinite(bid) or not math.isfinite(offer):
            self._state.dropped_ticks += 1
            return False, "NaN/inf bid or offer"
        if bid <= 0 or offer <= 0:
            self._state.dropped_ticks += 1
            return False, "non-positive price"
        if offer < bid:
            self._state.dropped_ticks += 1
            return False, "inverted spread"

        epic = str(getattr(quote, "epic", "") or "")
        if epic and any(ord(c) < 32 for c in epic):
            self._state.dropped_ticks += 1
            return False, "corrupted epic string"

        ts = getattr(quote, "time", None)
        if ts is not None:
            try:
                if isinstance(ts, datetime):
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_ms = (datetime.now(timezone.utc) - ts).total_seconds() * 1000.0
                else:
                    age_ms = 0.0
                if age_ms > MAX_TICK_LATENCY_MS:
                    self._state.dropped_ticks += 1
                    return False, f"stale tick {age_ms:.0f}ms > {MAX_TICK_LATENCY_MS:.0f}ms"
            except Exception:
                self._state.dropped_ticks += 1
                return False, "unparseable timestamp"

        self._state.accepted_ticks += 1
        return True, "ok"

    def assert_feature_shape(
        self,
        live_vector: Any,
        *,
        expected_shape: tuple[int, ...],
    ) -> tuple[bool, Any, str]:
        """
        Assert live feature vector matches training matrix dimensions.
        Returns (ok, vector_or_fallback, detail).
        """
        try:
            import numpy as np

            arr = np.asarray(live_vector, dtype=np.float64).reshape(-1)
        except Exception as exc:
            self._state.dropped_ticks += 1
            return self._fallback_vector(f"reshape failed: {exc}")

        if arr.size == 0:
            return self._fallback_vector("empty feature vector")

        if not np.all(np.isfinite(arr)):
            self._state.dropped_ticks += 1
            return self._fallback_vector("non-finite feature values")

        shape = (int(arr.size),)
        if expected_shape and shape[0] != int(expected_shape[0]):
            self._state.dropped_ticks += 1
            return self._fallback_vector(
                f"shape mismatch live={shape} expected={expected_shape}"
            )

        self._state.last_valid_shape = shape
        self._state.last_valid_features = {f"f{i}": float(v) for i, v in enumerate(arr)}
        return True, arr, "tensor_aligned"

    def _fallback_vector(self, reason: str) -> tuple[bool, Any, str]:
        if not self._state.last_valid_features:
            return False, None, reason
        try:
            import numpy as np

            vals = [self._state.last_valid_features[k] for k in sorted(self._state.last_valid_features)]
            return False, np.asarray(vals, dtype=np.float64), f"fallback:{reason}"
        except Exception:
            return False, None, reason


_GLOBAL_FILTER: TickIntegrityFilter | None = None


def get_tick_integrity_filter() -> TickIntegrityFilter:
    global _GLOBAL_FILTER
    if _GLOBAL_FILTER is None:
        _GLOBAL_FILTER = TickIntegrityFilter()
    return _GLOBAL_FILTER


def reset_tick_integrity_for_tests() -> None:
    global _GLOBAL_FILTER
    _GLOBAL_FILTER = None
