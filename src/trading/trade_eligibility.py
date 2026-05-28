"""
Time-based trade eligibility countdown for dashboard Live tab.

Approximate timers only — does not imply a trade will fire when the timer ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from signals.indicators import floor_time
from signals.signal_engine import SignalResult
from system.market_watch.calendar import get_market_status
from trading.session_manager import COLD_START_BAR_MINUTES, COLD_START_BARS, SessionManager

EligibilityKind = Literal[
    "cold_start",
    "signal_warmup",
    "next_bar",
    "session_closed",
    "blocked",
    "none",
]

SIGNAL_WARMUP_BARS = 4
BAR_MINUTES = 5


@dataclass(frozen=True)
class TradeEligibility:
    kind: EligibilityKind
    label: str
    remaining_sec: float | None
    display: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "remaining_sec": self.remaining_sec,
            "display": self.display,
        }


def format_duration_display(seconds: float, *, approximate: bool = True) -> str:
    """Human-readable duration; prefix ~ when approximate."""
    s = max(0, int(round(seconds)))
    prefix = "~" if approximate else ""
    if s >= 600:
        mins = max(1, (s + 29) // 60)
        return f"{prefix}{mins} min"
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{prefix}{m}:{sec:02d}"
    return f"{prefix}0:{s:02d}"


def next_bar_close_at(dt: datetime, *, bar_minutes: int = BAR_MINUTES) -> datetime:
    """Wall-clock next 5m bar close boundary after *dt*."""
    start = floor_time(dt, bar_minutes)
    return start + timedelta(minutes=bar_minutes)


def seconds_until_bar_close(dt: datetime, *, bar_minutes: int = BAR_MINUTES) -> float:
    return max(0.0, (next_bar_close_at(dt, bar_minutes=bar_minutes) - dt).total_seconds())


def cold_start_remaining_sec(
    session: SessionManager,
    *,
    at: datetime | None = None,
) -> float:
    bars = int(session.bars_since_open(at=at))
    bars_left = max(0, COLD_START_BARS - bars)
    if bars_left <= 0:
        return 0.0
    return float(bars_left * COLD_START_BAR_MINUTES * 60)


def signal_warmup_remaining_sec(
    signal_engine: Any,
    market: str,
    *,
    at: datetime | None = None,
) -> float | None:
    """Approximate seconds until 4×5m bars exist; None if engine unavailable."""
    try:
        df = signal_engine.quote_df(market)
        c5 = signal_engine.candles(df, BAR_MINUTES)
        have = len(c5)
    except Exception:
        return None
    if have >= SIGNAL_WARMUP_BARS:
        return 0.0
    bars_left = SIGNAL_WARMUP_BARS - have
    now = at or datetime.now()
    to_next = seconds_until_bar_close(now)
    return float((bars_left - 1) * BAR_MINUTES * 60 + to_next) if bars_left > 0 else 0.0


def _gate_by_name(gates: list[Any], name: str) -> Any | None:
    for g in gates:
        if getattr(g, "name", None) == name:
            return g
    return None


def _score_block_display(block_reason: str) -> TradeEligibility:
    reason = (block_reason or "conditions not met").strip()
    return TradeEligibility(
        kind="blocked",
        label=reason,
        remaining_sec=None,
        display=f"Blocked: {reason} (no timer)",
    )


def _time_eligibility(
    kind: EligibilityKind,
    label: str,
    remaining_sec: float,
    *,
    display: str | None = None,
) -> TradeEligibility:
    dur = format_duration_display(remaining_sec)
    return TradeEligibility(
        kind=kind,
        label=label,
        remaining_sec=round(remaining_sec, 1),
        display=display or dur,
    )


def build_trade_eligibility(
    *,
    gates: list[Any],
    session: SessionManager,
    signal_engine: Any | None,
    market: str,
    epic: str,
    block_reason: str,
    sig: SignalResult | None,
    now: datetime,
    quote_ts: datetime | None = None,
) -> TradeEligibility | None:
    """
    Pick a single countdown line for the Live tab.

    Time-based blockers take priority over score-based; among time-based,
    session closed > cold start > signal warmup > next bar.
    """
    session_gate = _gate_by_name(gates, "session_open")
    if session_gate is not None and not bool(getattr(session_gate, "passed", False)):
        status = get_market_status(epic, at=now)
        if status and status.next_open_at:
            nxt = status.next_open_at
            if nxt.tzinfo is not None and now.tzinfo is None:
                now_cmp = now.replace(tzinfo=nxt.tzinfo)
            elif nxt.tzinfo is None and now.tzinfo is not None:
                now_cmp = now
                nxt = nxt.replace(tzinfo=now.tzinfo)
            else:
                now_cmp = now
            remaining = max(0.0, (nxt - now_cmp).total_seconds())
            label = "Session closed"
            disp = format_duration_display(remaining)
            if status.display_name:
                label = f"{status.display_name} closed"
            return TradeEligibility(
                kind="session_closed",
                label=label,
                remaining_sec=round(remaining, 1),
                display=f"Opens in {disp}",
            )
        return TradeEligibility(
            kind="session_closed",
            label="Session closed",
            remaining_sec=None,
            display="Session closed (open time unknown)",
        )

    cold_gate = _gate_by_name(gates, "cold_start_gap")
    if cold_gate is not None and not bool(getattr(cold_gate, "passed", False)):
        value = getattr(cold_gate, "value", None) or {}
        if isinstance(value, dict) and value.get("gap"):
            detail = str(getattr(cold_gate, "detail", "") or "gap open")
            return _score_block_display(detail)
        if session.is_cold_start(at=now):
            remaining = cold_start_remaining_sec(session, at=now)
            if remaining > 0:
                bars = session.bars_since_open(at=now)
                dur = format_duration_display(remaining)
                return TradeEligibility(
                    kind="cold_start",
                    label="Cold start",
                    remaining_sec=round(remaining, 1),
                    display=f"Cold start {dur} ({bars}/{COLD_START_BARS} bars)",
                )

    notes = (sig.notes if sig else "") or ""
    notes_lower = notes.lower()
    if "collecting live data" in notes_lower:
        if signal_engine is not None:
            rem = signal_warmup_remaining_sec(signal_engine, market, at=now)
            if rem is not None and rem > 0:
                return _time_eligibility(
                    "signal_warmup",
                    "Signal warming",
                    rem,
                    display=f"Signal warming {format_duration_display(rem)}",
                )
        return _time_eligibility(
            "signal_warmup",
            "Signal warming",
            float(BAR_MINUTES * 60),
            display=f"Signal warming {format_duration_display(float(BAR_MINUTES * 60))}",
        )

    if "duplicate suppressed" in notes_lower or (
        block_reason and "next closed" in block_reason.lower()
    ):
        ref = quote_ts if quote_ts is not None else now
        rem = seconds_until_bar_close(ref)
        if rem > 0:
            return _time_eligibility(
                "next_bar",
                "Next 5m bar close",
                rem,
                display=f"Next bar close {format_duration_display(rem)}",
            )

    br = (block_reason or "").strip().lower()
    if br and any(
        x in br
        for x in (
            "rsi",
            "fitness",
            "threshold",
            "score",
            "vol regime",
            "blocked:",
            "points",
            "spread",
            "risk",
            "pause",
            "stop",
            "gap",
            "no buy/sell",
        )
    ):
        return _score_block_display(block_reason)

    fitness_gate = _gate_by_name(gates, "environment_fitness")
    if fitness_gate is not None and not bool(getattr(fitness_gate, "passed", False)):
        return _score_block_display(
            str(getattr(fitness_gate, "detail", "") or "environment fitness")
        )

    points_gate = _gate_by_name(gates, "points_state")
    if points_gate is not None and not bool(getattr(points_gate, "passed", False)):
        return _score_block_display(str(getattr(points_gate, "detail", "") or "points"))

    risk_gate = _gate_by_name(gates, "risk_validation")
    if risk_gate is not None and not bool(getattr(risk_gate, "passed", False)):
        return _score_block_display(str(getattr(risk_gate, "detail", "") or "risk"))

    sig_gate = _gate_by_name(gates, "signal_confidence")
    if sig_gate is not None and not bool(getattr(sig_gate, "passed", False)):
        if block_reason:
            return _score_block_display(block_reason)

    return None
