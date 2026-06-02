"""Gate trading-loop evaluation until the market stream delivers its first tick."""

from __future__ import annotations

import threading

from system.engine_log import log_engine

_DEFAULT_WAIT_SEC = 120.0
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


def wait_stream_ready(timeout: float | None = None) -> bool:
    if _ready.is_set():
        return True
    ok = _ready.wait(timeout=timeout if timeout is not None else _DEFAULT_WAIT_SEC)
    if not ok:
        log_engine(
            f"stream_ready: timeout after {timeout or _DEFAULT_WAIT_SEC:.0f}s — "
            "trading loops will evaluate without live stream"
        )
    return ok
