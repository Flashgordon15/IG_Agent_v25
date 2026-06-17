"""
Peak-equity drawdown tracker.

Professional trading systems track:
  - Peak equity (high-water mark) since session start
  - Current drawdown from peak (£ and %)
  - Maximum drawdown this session
  - Alert when drawdown exceeds configurable threshold

This runs passively; the trading loop calls update() on each balance refresh.
Nothing here blocks trading — it is read-only monitoring.

Session P&L uses Python ``decimal`` to prevent float drift on drawdown rules.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal

from system.balance_pnl_decimal import decimal_to_float, money_decimal
from system.engine_log import log_engine

_lock = threading.RLock()  # Reentrant — update() calls snapshot() from within its lock

_TWOPLACES = Decimal("0.01")


@dataclass
class DrawdownState:
    peak_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    current_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    max_drawdown_gbp: Decimal = field(default_factory=lambda: Decimal("0"))
    max_drawdown_pct: Decimal = field(default_factory=lambda: Decimal("0"))
    session_start_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    alert_threshold_pct: float = 5.0  # alert when drawdown > 5% of peak
    alert_sent: bool = False
    observations: int = 0
    last_balance_field_used: str = "balance"


_state: DrawdownState = DrawdownState()


def operational_status() -> str:
    """
    Monitor lifecycle for ops verification (not the Superjet breach latch).

    STANDBY — no balance observations yet (session not seeded).
    NOMINAL — tracking active, no drawdown alert latched.
    ALERT   — peak-equity alert threshold breached (informational).
    """
    with _lock:
        if _state.observations <= 0:
            return "STANDBY"
        if _state.alert_sent:
            return "ALERT"
        return "NOMINAL"


def snapshot_for_telemetry() -> dict[str, str | float | int | None]:
    """JSON-safe block for Flight Deck / HUD (all floats, no Decimal objects)."""
    debug = snapshot_decimal_debug()
    debug["operational_status"] = operational_status()
    return debug


def configure(*, alert_threshold_pct: float = 5.0) -> None:
    with _lock:
        _state.alert_threshold_pct = float(alert_threshold_pct)


def reset_session(balance: float | Decimal, *, field: str = "balance") -> None:
    with _lock:
        b = money_decimal(balance, field="session_reset_balance")
        if b is None:
            return
        _state.peak_balance = b
        _state.current_balance = b
        _state.session_start_balance = b
        _state.max_drawdown_gbp = Decimal("0")
        _state.max_drawdown_pct = Decimal("0")
        _state.alert_sent = False
        _state.observations = 0
        _state.last_balance_field_used = str(field or "balance")


def update(
    balance: float | Decimal,
    *,
    field: str = "balance",
) -> dict[str, float]:
    """
    Record a new balance reading. Returns current drawdown snapshot.

    Call this whenever the account balance is refreshed (e.g. position sync tick).
    *field* must be ``balance`` for session P&L used by drawdown guards — never
    ``available`` alone (margin-reserved cash ≠ equity).
    """
    b = money_decimal(balance, field=f"drawdown_update.{field}")
    if b is None:
        return snapshot()
    with _lock:
        _state.current_balance = b
        _state.last_balance_field_used = str(field or "balance")
        _state.observations += 1
        if _state.peak_balance <= 0:
            _state.peak_balance = b
            _state.session_start_balance = b

        if b > _state.peak_balance:
            _state.peak_balance = b
            _state.alert_sent = False  # reset alert when new peak reached

        dd_gbp = max(Decimal("0"), _state.peak_balance - b)
        dd_pct = (
            (dd_gbp / _state.peak_balance * Decimal("100"))
            if _state.peak_balance > 0
            else Decimal("0")
        )

        if dd_gbp > _state.max_drawdown_gbp:
            _state.max_drawdown_gbp = dd_gbp
            _state.max_drawdown_pct = dd_pct

        threshold = Decimal(str(_state.alert_threshold_pct))
        if dd_pct >= threshold and not _state.alert_sent:
            _state.alert_sent = True
            peak_str = f"£{decimal_to_float(_state.peak_balance):.0f}"
            cur_f = decimal_to_float(b)
            dd_f = decimal_to_float(dd_gbp)
            msg = (
                f"DRAWDOWN ALERT: {decimal_to_float(dd_pct):.1f}% from peak {peak_str} "
                f"(current £{cur_f:.0f}, down £{dd_f:.0f})"
            )
            log_engine(msg)
            tg_msg = (
                f"Drawdown {decimal_to_float(dd_pct):.1f}% — peak {peak_str} → "
                f"current £{cur_f:.0f} (£{dd_f:.0f} down)"
            )

            def _send_tg() -> None:
                try:
                    from system.telegram_notifier import get_telegram_notifier

                    notifier = get_telegram_notifier()
                    if notifier and notifier.enabled:
                        notifier.send_alert(tg_msg, dedupe_key="drawdown_alert")
                except Exception:
                    pass

            import threading as _threading

            _threading.Thread(target=_send_tg, daemon=True, name="dd-alert").start()

        return snapshot()


def snapshot_decimal_debug() -> dict[str, str | float | int | None]:
    with _lock:
        peak = _state.peak_balance
        cur = _state.current_balance
        start = _state.session_start_balance
        session_pnl = (cur - start).quantize(_TWOPLACES) if start > 0 else Decimal("0")
        dd = max(Decimal("0"), peak - cur)
        pct = (dd / peak * Decimal("100")) if peak > 0 else Decimal("0")
        return {
            "peak_balance_decimal": str(peak),
            "current_balance_decimal": str(cur),
            "session_start_balance_decimal": str(start),
            "session_pnl_decimal": str(session_pnl),
            "session_pnl_gbp": decimal_to_float(session_pnl),
            "drawdown_gbp_decimal": str(dd),
            "drawdown_pct": decimal_to_float(pct),
            "last_balance_field_used": _state.last_balance_field_used,
            "observations": _state.observations,
        }


def snapshot() -> dict[str, float]:
    with _lock:
        peak = _state.peak_balance
        cur = _state.current_balance
        start = _state.session_start_balance
        dd = max(Decimal("0"), peak - cur)
        pct = (dd / peak * Decimal("100")) if peak > 0 else Decimal("0")
        session_pnl = (cur - start).quantize(_TWOPLACES)
        return {
            "peak_balance": decimal_to_float(peak),
            "current_balance": decimal_to_float(cur),
            "drawdown_gbp": decimal_to_float(dd),
            "drawdown_pct": decimal_to_float(pct),
            "max_drawdown_gbp": decimal_to_float(_state.max_drawdown_gbp),
            "max_drawdown_pct": decimal_to_float(_state.max_drawdown_pct),
            "session_start_balance": decimal_to_float(start),
            "session_pnl_gbp": decimal_to_float(session_pnl),
        }
