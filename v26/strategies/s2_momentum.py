"""
S2 — momentum breakout (shadow only, Phase 1).

Fires on ``bar_close`` when range expansion + directional close vs bar range.
Uses feeder OHLC only — no live SignalEngine dependency.
"""

from __future__ import annotations

from typing import Any

from strategies.base import ShadowIntent

STRATEGY_ID = "S2_momentum"

# Minimum close position in bar range (top/bottom 25%) for direction
_RANGE_EDGE = 0.75
_DEFAULT_MIN_RANGE_PCT = 0.0008


class S2Momentum:
    strategy_id = STRATEGY_ID

    def __init__(self, *, min_range_pct: float | None = None) -> None:
        self._override_min_range_pct = min_range_pct

    def _min_range_pct(self, epic: str) -> float:
        if self._override_min_range_pct is not None:
            return self._override_min_range_pct
        try:
            from strategies.s2_config import get_s2_min_range_pct

            return get_s2_min_range_pct(epic)
        except ImportError:
            return _DEFAULT_MIN_RANGE_PCT

    def evaluate_feeder_event(self, row: dict[str, Any]) -> ShadowIntent | None:
        if str(row.get("event_type") or "") != "bar_close":
            return None
        payload = row.get("payload") or {}
        try:
            o = float(payload.get("open") or 0)
            h = float(payload.get("high") or 0)
            lo = float(payload.get("low") or 0)
            c = float(payload.get("close") or 0)
        except (TypeError, ValueError):
            return None
        if c <= 0 or h <= lo:
            return None
        bar_range = h - lo
        if bar_range <= 0:
            return None
        epic = str(row.get("epic") or "")
        min_range = self._min_range_pct(epic)
        if bar_range / c < min_range:
            return None

        pos = (c - lo) / bar_range
        direction = "WAIT"
        confidence = 0.0
        reason = "S2: no breakout"
        if pos >= _RANGE_EDGE and c > o:
            direction = "BUY"
            confidence = min(99.0, 55.0 + (pos - _RANGE_EDGE) * 80.0)
            reason = f"S2: bullish breakout close@{pos:.0%} of range"
        elif pos <= (1.0 - _RANGE_EDGE) and c < o:
            direction = "SELL"
            confidence = min(99.0, 55.0 + ((1.0 - _RANGE_EDGE) - pos) * 80.0)
            reason = f"S2: bearish breakout close@{pos:.0%} of range"

        would_trade = direction in ("BUY", "SELL") and confidence >= 55.0
        session = str(row.get("session") or "")
        setup_key = f"{direction}|momentum|{session}|range{int(bar_range)}"

        return ShadowIntent(
            strategy_id=self.strategy_id,
            epic=epic,
            market=str(row.get("market") or ""),
            session=session,
            direction=direction,
            would_trade=would_trade,
            confidence=confidence,
            setup_key=setup_key,
            source_ts=str(row.get("ts") or ""),
            reason=reason,
            payload={
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "bar_range": round(bar_range, 4),
                "range_pos": round(pos, 4),
                "min_range_pct": min_range,
            },
        )
