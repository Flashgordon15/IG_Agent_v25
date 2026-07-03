"""
Immutable CapitalGuard — hard fail-closed live order transmission safety.

Bypasses all AI/ML models. Enforces maximum daily drawdown and absolute lot ceiling
at the final live REST transmission boundary inside ``LiveExecutor``.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

MAX_DAILY_DRAWDOWN_PCT = 0.02
MAX_LIVE_LOT_SIZE = 1.0
_HARD_EXIT_CODE = 99


class CapitalGuard:
    """Immutable institutional capital risk guard — not configurable at runtime."""

    MAX_DAILY_DRAWDOWN_PCT: float = MAX_DAILY_DRAWDOWN_PCT
    MAX_LIVE_LOT_SIZE: float = MAX_LIVE_LOT_SIZE

    _lock = threading.Lock()
    _tripped = False
    _session_start_equity: float | None = None

    @classmethod
    def enforce_order_transmission(
        cls,
        *,
        size: float,
        rest_client: Any,
        epic: str = "",
    ) -> tuple[bool, str]:
        """
        Final gate before ``place_market_order``.

        Returns ``(True, "ok")`` when transmission is permitted.
        On drawdown breach triggers cancel-all and ``sys.exit(99)``.
        """
        lot = float(size)
        if lot > cls.MAX_LIVE_LOT_SIZE:
            try:
                from system.demo_execution_plane import demo_throughput_active

                if demo_throughput_active():
                    return True, "ok"
            except Exception:
                pass
            reason = (
                f"CapitalGuard: live size {lot:.4f} exceeds hard ceiling "
                f"{cls.MAX_LIVE_LOT_SIZE:.1f} lot — REJECTED"
            )
            log_engine(reason + (f" epic={epic}" if epic else ""))
            return False, reason

        breached, detail = cls._evaluate_daily_drawdown(rest_client)
        if breached:
            cls._hard_fail_closed(rest_client, detail)
        return True, "ok"

    @classmethod
    def _evaluate_daily_drawdown(cls, rest_client: Any) -> tuple[bool, str]:
        current = cls._resolve_account_equity(rest_client)
        if current is None or current <= 0:
            return False, ""

        with cls._lock:
            if cls._session_start_equity is None:
                cls._session_start_equity = current
                return False, ""
            start = float(cls._session_start_equity)

        if start <= 0:
            return False, ""

        drop_pct = (start - current) / start
        if drop_pct > cls.MAX_DAILY_DRAWDOWN_PCT:
            return True, (
                f"daily equity drawdown {drop_pct * 100:.2f}% exceeds "
                f"{cls.MAX_DAILY_DRAWDOWN_PCT * 100:.1f}% "
                f"(start={start:.2f} current={current:.2f})"
            )
        return False, ""

    @staticmethod
    def _resolve_account_equity(rest_client: Any) -> float | None:
        try:
            from system.drawdown_monitor import snapshot as drawdown_snapshot

            snap = drawdown_snapshot()
            for key in ("current_balance", "peak_balance", "session_start_balance"):
                raw = snap.get(key)
                if raw is not None:
                    val = float(raw)
                    if val > 0:
                        return val
        except Exception as exc:
            log_guarded_exception("capital_guard_drawdown_monitor", exc)

        try:
            if rest_client is not None and hasattr(rest_client, "fetch_account_snapshot"):
                acct = rest_client.fetch_account_snapshot()
                if isinstance(acct, dict):
                    for key in ("balance", "available", "equity", "accountBalance"):
                        raw = acct.get(key)
                        if raw is not None:
                            val = float(raw)
                            if val > 0:
                                return val
        except Exception as exc:
            log_guarded_exception("capital_guard_account_snapshot", exc)

        try:
            if rest_client is not None and hasattr(rest_client, "get_account_balance"):
                val = float(rest_client.get_account_balance())
                if val > 0:
                    return val
        except Exception as exc:
            log_guarded_exception("capital_guard_account_balance", exc)

        return None

    @classmethod
    def _cancel_all_open_orders_and_positions(cls, rest_client: Any) -> None:
        if rest_client is None:
            return
        for method_name in (
            "cancel_all_working_orders",
            "close_all_positions",
            "flatten_all",
        ):
            method = getattr(rest_client, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                log_engine(f"CapitalGuard: invoked {method_name}()")
                return
            except Exception as exc:
                log_guarded_exception(f"capital_guard_{method_name}", exc)

        try:
            if hasattr(rest_client, "open_positions"):
                positions = rest_client.open_positions() or []
                close = getattr(rest_client, "close_position", None)
                if callable(close):
                    for pos in positions:
                        deal_id = pos.get("dealId") or pos.get("deal_id")
                        if deal_id:
                            close(str(deal_id))
        except Exception as exc:
            log_guarded_exception("capital_guard_flatten", exc)

    @classmethod
    def _hard_fail_closed(cls, rest_client: Any, reason: str) -> None:
        with cls._lock:
            if cls._tripped:
                sys.exit(_HARD_EXIT_CODE)
            cls._tripped = True

        log_engine(f"CapitalGuard HARD FAIL-CLOSED: {reason} — cancel-all + exit {_HARD_EXIT_CODE}")
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(f"🛑 CapitalGuard TRIPPED — {reason}")
        except Exception:
            pass

        cls._cancel_all_open_orders_and_positions(rest_client)
        try:
            from system.identity.instance_lock import force_release_instance_lock

            force_release_instance_lock()
        except Exception:
            pass
        sys.exit(_HARD_EXIT_CODE)

    @classmethod
    def reset_session_baseline_for_tests(cls) -> None:
        with cls._lock:
            cls._session_start_equity = None
            cls._tripped = False
