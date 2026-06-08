"""
S3 — session FX mean-reversion (shadow only, Stage 2).

Fades bar-close extremes during London / London–US overlap on EUR/USD and GBP/USD.
Inverse of S2: sell strength at top of bar range, buy weakness at bottom.
"""

from __future__ import annotations

from typing import Any

from strategies.base import ShadowIntent

STRATEGY_ID = "S3_session_fx"

FX_EPICS = frozenset(
    {
        "CS.D.EURUSD.CFD.IP",
        "CS.D.GBPUSD.CFD.IP",
    }
)
FX_SESSIONS = frozenset(
    {
        "london_morning",
        "london_us_overlap",
        "us_morning",
        "us_afternoon",  # session_name() UTC bucket; alias for US-session fades
    }
)

_RANGE_EDGE = 0.80
_MIN_RANGE_PCT = 0.00015


class S3SessionFx:
    strategy_id = STRATEGY_ID

    def evaluate_feeder_event(self, row: dict[str, Any]) -> ShadowIntent | None:
        if str(row.get("event_type") or "") != "bar_close":
            return None
        epic = str(row.get("epic") or "")
        session = str(row.get("session") or "")
        if epic not in FX_EPICS or session not in FX_SESSIONS:
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
        if bar_range <= 0 or bar_range / c < _MIN_RANGE_PCT:
            return None

        pos = (c - lo) / bar_range
        direction = "WAIT"
        confidence = 0.0
        reason = "S3: no fade setup"
        if pos >= _RANGE_EDGE:
            direction = "SELL"
            confidence = min(92.0, 52.0 + (pos - _RANGE_EDGE) * 100.0)
            reason = f"S3: fade strength close@{pos:.0%} of range"
        elif pos <= (1.0 - _RANGE_EDGE):
            direction = "BUY"
            confidence = min(92.0, 52.0 + ((1.0 - _RANGE_EDGE) - pos) * 100.0)
            reason = f"S3: fade weakness close@{pos:.0%} of range"

        would_trade = direction in ("BUY", "SELL") and confidence >= 52.0
        setup_key = f"{direction}|meanrev|{session}|range{int(bar_range * 10000)}"

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
                "bar_range": round(bar_range, 6),
                "range_pos": round(pos, 4),
                "fade_mode": "session_mean_reversion",
            },
        )
