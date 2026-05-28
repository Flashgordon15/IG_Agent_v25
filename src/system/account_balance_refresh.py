"""Background account balance refresh for dashboard snapshot."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from system.engine_log import log_engine

_INTERVAL_OPEN_SEC = 300.0
_INTERVAL_FLAT_SEC = 1800.0


class AccountBalanceRefresher:
    def __init__(
        self,
        rest_client: Any,
        *,
        open_count_fn: Callable[[], int],
        on_balance: Callable[[float | None, float | None], None] | None = None,
    ) -> None:
        self._rest = rest_client
        self._open_count_fn = open_count_fn
        self._on_balance = on_balance
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def maybe_refresh(self, *, force: bool = False) -> dict[str, float | None]:
        now = time.time()
        try:
            open_n = int(self._open_count_fn())
        except Exception:
            open_n = 0
        interval = _INTERVAL_OPEN_SEC if open_n > 0 else _INTERVAL_FLAT_SEC
        with self._lock:
            if not force and (now - self._last_refresh) < interval:
                return self._cached_summary()
            self._last_refresh = now
        summary: dict[str, float | None] = {}
        try:
            if hasattr(self._rest, "maybe_refresh_account_summary"):
                summary = self._rest.maybe_refresh_account_summary(min_interval=0.0)
            elif hasattr(self._rest, "fetch_account_balance"):
                bal = float(self._rest.fetch_account_balance())
                summary = {"balance": bal, "available": bal}
        except Exception:
            return self._cached_summary()
        bal = summary.get("balance")
        avail = summary.get("available")
        try:
            if bal is not None:
                log_engine(
                    f"Account balance refresh: £{float(bal):.2f} "
                    f"(available £{float(avail or bal):.2f}, open={open_n})"
                )
        except (TypeError, ValueError):
            log_engine("Account balance refresh: updated")
        if self._on_balance is not None:
            try:
                self._on_balance(
                    float(bal) if bal is not None else None,
                    float(avail) if avail is not None else None,
                )
            except Exception:
                pass
        with self._lock:
            self._last_summary = dict(summary)
        return summary

    def _cached_summary(self) -> dict[str, float | None]:
        with self._lock:
            return dict(getattr(self, "_last_summary", {}))
