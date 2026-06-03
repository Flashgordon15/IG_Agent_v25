"""Gate trading-loop evaluation until the market stream delivers its first tick."""

from __future__ import annotations

import threading
import time

from system.engine_log import log_engine, record_engine_warning

_DEFAULT_WAIT_SEC = 120.0
_HUB_POLL_SEC = 2.0
_ready = threading.Event()


def reset_stream_ready() -> None:
    _ready.clear()


def signal_stream_ready(*, source: str = "") -> None:
    if _ready.is_set():
        return
    _ready.set()
    detail = f" ({source})" if source else ""
    log_engine(f"stream_ready: market stream live{detail}")


def is_stream_ready() -> bool:
    return _ready.is_set()


def _hub_has_live_quotes(*, max_age_sec: float = 30.0) -> bool:
    """True when MarketDataHub already has a fresh bid/offer (e.g. after restart)."""
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        for epic in hub.list_epics():
            snap = hub.get_snapshot(epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                continue
            if snap.age_seconds() <= max_age_sec:
                return True
    except Exception:
        pass
    return False


def wait_stream_ready(timeout: float | None = None) -> bool:
    if _ready.is_set():
        return True
    effective_timeout = float(timeout if timeout is not None else _DEFAULT_WAIT_SEC)
    deadline = time.monotonic() + effective_timeout
    while not _ready.is_set():
        if _hub_has_live_quotes():
            log_engine(
                "stream_ready: hub quotes already live — proceeding without stream signal"
            )
            signal_stream_ready(source="hub_live_recovery")
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _ready.wait(timeout=min(_HUB_POLL_SEC, max(0.05, remaining)))
    if _ready.is_set():
        return True
    record_engine_warning(
        "stream_ready_timeout",
        f"no stream signal within {effective_timeout:.0f}s — trading loops will proceed",
    )
    signal_stream_ready(source="timeout_proceed")
    return False
