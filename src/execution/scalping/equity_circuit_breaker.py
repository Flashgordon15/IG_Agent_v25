"""Daily equity drawdown circuit breaker — flatten, cancel, lock until reset."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from execution.scalping.config import scalping_settings
from system.engine_log import log_engine

_cb_lock = threading.Lock()
_breaker: EquityCircuitBreaker | None = None


class EquityCircuitBreaker:
    def __init__(self, *, drawdown_pct: float = 1.5) -> None:
        self._drawdown_pct = float(drawdown_pct)
        self._lock = threading.Lock()
        self._day_key: str = ""
        self._start_equity: float | None = None
        self._tripped: bool = False
        self._trip_reason: str = ""
        self._tripped_at: float = 0.0

    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _maybe_roll_day(self) -> None:
        day = self._utc_day()
        if day != self._day_key:
            self._day_key = day
            self._start_equity = None
            self._tripped = False
            self._trip_reason = ""
            self._tripped_at = 0.0
            log_engine(f"SCALPING equity circuit breaker daily reset ({day})")

    def refresh_baseline(self, equity: float) -> None:
        if equity <= 0:
            return
        with self._lock:
            self._maybe_roll_day()
            if self._start_equity is None:
                self._start_equity = float(equity)
                log_engine(
                    f"SCALPING daily equity baseline set: £{self._start_equity:.2f}"
                )

    def current_drawdown_pct(self, equity: float) -> float | None:
        with self._lock:
            self._maybe_roll_day()
            if self._start_equity is None or self._start_equity <= 0:
                return None
            if equity <= 0:
                return None
            loss = self._start_equity - float(equity)
            if loss <= 0:
                return 0.0
            return (loss / self._start_equity) * 100.0

    def is_locked(self) -> bool:
        with self._lock:
            self._maybe_roll_day()
            return self._tripped

    def lock_reason(self) -> str:
        with self._lock:
            return self._trip_reason if self._tripped else ""

    def check_equity(self, equity: float) -> tuple[bool, str]:
        """Return (allowed, message). allowed=False when circuit tripped or just tripped."""
        with self._lock:
            self._maybe_roll_day()
            if self._tripped:
                return False, self._trip_reason or "Daily equity drawdown circuit active"
        self.refresh_baseline(equity)
        dd = self.current_drawdown_pct(equity)
        if dd is None:
            return True, ""
        if dd < self._drawdown_pct:
            return True, ""
        return False, (
            f"Daily equity drawdown {dd:.2f}% >= {self._drawdown_pct:.2f}% limit"
        )

    def trip(
        self,
        client: Any,
        *,
        equity: float,
        reason: str,
    ) -> None:
        with self._lock:
            if self._tripped:
                return
            self._tripped = True
            self._trip_reason = str(reason)
            self._tripped_at = time.time()
        log_engine(f"SCALPING EQUITY CIRCUIT BREAKER TRIPPED — {reason}")
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(f"Equity circuit breaker: {reason}")
        except Exception:
            pass
        self._execute_trip_actions(client)

    def maybe_trip_from_equity(self, client: Any, equity: float) -> bool:
        allowed, msg = self.check_equity(equity)
        if allowed:
            return False
        if not self.is_locked():
            self.trip(client, equity=equity, reason=msg)
        return True

    def _execute_trip_actions(self, client: Any) -> None:
        if client is None:
            return
        try:
            if hasattr(client, "cancel_all_working_orders"):
                n = client.cancel_all_working_orders()
                log_engine(f"SCALPING circuit breaker cancelled {n} working orders")
        except Exception as e:
            log_engine(f"SCALPING cancel working orders failed: {e}")
        try:
            if hasattr(client, "flatten_all_positions"):
                n = client.flatten_all_positions()
                log_engine(f"SCALPING circuit breaker flattened {n} positions")
        except Exception as e:
            log_engine(f"SCALPING flatten all failed: {e}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "day": self._day_key,
                "start_equity": self._start_equity,
                "drawdown_limit_pct": self._drawdown_pct,
                "tripped": self._tripped,
                "reason": self._trip_reason,
                "tripped_at": self._tripped_at,
            }


def _account_equity(client: Any) -> float | None:
    if client is None:
        return None
    try:
        if hasattr(client, "maybe_refresh_account_summary"):
            summary = client.maybe_refresh_account_summary(min_interval=30.0)
        elif hasattr(client, "get_cached_account_summary"):
            summary = client.get_cached_account_summary()
        else:
            summary = {}
        bal = summary.get("balance")
        pnl = summary.get("profit_loss")
        if bal is not None:
            equity = float(bal)
            if pnl is not None:
                equity = float(bal) + float(pnl)
            return equity
        if hasattr(client, "fetch_account_balance"):
            b = client.fetch_account_balance()
            return float(b) if b is not None else None
    except Exception:
        return None
    return None


def get_equity_circuit_breaker() -> EquityCircuitBreaker:
    global _breaker
    with _cb_lock:
        if _breaker is None:
            s = scalping_settings()
            _breaker = EquityCircuitBreaker(
                drawdown_pct=float(s.get("daily_equity_drawdown_pct", 1.5)),
            )
        return _breaker


def check_equity_circuit(client: Any) -> tuple[bool, str]:
    breaker = get_equity_circuit_breaker()
    equity = _account_equity(client)
    if equity is None:
        if breaker.is_locked():
            return False, breaker.lock_reason()
        return True, ""
    if breaker.maybe_trip_from_equity(client, equity):
        return False, breaker.lock_reason() or "Daily equity drawdown circuit"
    return breaker.check_equity(equity)


def reset_equity_circuit_for_tests() -> None:
    global _breaker
    with _cb_lock:
        _breaker = None
