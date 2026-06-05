"""
Peak-equity drawdown tracker.

Professional trading systems track:
  - Peak equity (high-water mark) since session start
  - Current drawdown from peak (£ and %)
  - Maximum drawdown this session
  - Alert when drawdown exceeds configurable threshold

This runs passively; the trading loop calls update() on each balance refresh.
Nothing here blocks trading — it is read-only monitoring.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from system.engine_log import log_engine

_lock = threading.RLock()  # Reentrant — update() calls snapshot() from within its lock


@dataclass
class DrawdownState:
    peak_balance: float = 0.0
    current_balance: float = 0.0
    max_drawdown_gbp: float = 0.0
    max_drawdown_pct: float = 0.0
    session_start_balance: float = 0.0
    alert_threshold_pct: float = 5.0  # alert when drawdown > 5% of peak
    alert_sent: bool = False
    observations: int = 0


_state: DrawdownState = DrawdownState()


def configure(*, alert_threshold_pct: float = 5.0) -> None:
    with _lock:
        _state.alert_threshold_pct = float(alert_threshold_pct)


def reset_session(balance: float) -> None:
    with _lock:
        b = float(balance)
        _state.peak_balance = b
        _state.current_balance = b
        _state.session_start_balance = b
        _state.max_drawdown_gbp = 0.0
        _state.max_drawdown_pct = 0.0
        _state.alert_sent = False
        _state.observations = 0


def update(balance: float) -> dict[str, float]:
    """
    Record a new balance reading. Returns current drawdown snapshot.

    Call this whenever the account balance is refreshed (e.g. position sync tick).
    """
    b = float(balance)
    with _lock:
        _state.current_balance = b
        _state.observations += 1
        if _state.peak_balance <= 0:
            _state.peak_balance = b
            _state.session_start_balance = b

        if b > _state.peak_balance:
            _state.peak_balance = b
            _state.alert_sent = False  # reset alert when new peak reached

        dd_gbp = max(0.0, _state.peak_balance - b)
        dd_pct = (dd_gbp / _state.peak_balance * 100.0) if _state.peak_balance > 0 else 0.0

        if dd_gbp > _state.max_drawdown_gbp:
            _state.max_drawdown_gbp = dd_gbp
            _state.max_drawdown_pct = dd_pct

        threshold = _state.alert_threshold_pct
        if dd_pct >= threshold and not _state.alert_sent:
            _state.alert_sent = True
            peak_str = f"£{_state.peak_balance:.0f}"
            msg = (
                f"DRAWDOWN ALERT: {dd_pct:.1f}% from peak {peak_str} "
                f"(current £{b:.0f}, down £{dd_gbp:.0f})"
            )
            log_engine(msg)
            tg_msg = (
                f"Drawdown {dd_pct:.1f}% — peak {peak_str} → current £{b:.0f} (£{dd_gbp:.0f} down)"
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


def snapshot() -> dict[str, float]:
    with _lock:
        peak = _state.peak_balance
        cur = _state.current_balance
        dd = max(0.0, peak - cur)
        pct = (dd / peak * 100.0) if peak > 0 else 0.0
        return {
            "peak_balance": round(peak, 2),
            "current_balance": round(cur, 2),
            "drawdown_gbp": round(dd, 2),
            "drawdown_pct": round(pct, 2),
            "max_drawdown_gbp": round(_state.max_drawdown_gbp, 2),
            "max_drawdown_pct": round(_state.max_drawdown_pct, 2),
            "session_start_balance": round(_state.session_start_balance, 2),
            "session_pnl_gbp": round(cur - _state.session_start_balance, 2),
        }
