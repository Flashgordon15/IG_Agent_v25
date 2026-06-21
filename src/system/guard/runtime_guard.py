"""Centralized runtime guard — full traceback logging, no silent swallowing."""

from __future__ import annotations

import traceback
from typing import Any, Callable, TypeVar

from system.engine_log import log_engine

T = TypeVar("T")


def log_guarded_exception(
    subsystem: str,
    exc: BaseException,
    *,
    epic: str = "",
    detail: str = "",
) -> None:
    """Log subsystem failure with full stack trace (fail-loud, never silent)."""
    epic_bit = f" epic={epic}" if epic else ""
    detail_bit = f" {detail}" if detail else ""
    tb = traceback.format_exc()
    log_engine(
        f"runtime_guard:{subsystem}{epic_bit}{detail_bit} "
        f"{type(exc).__name__}: {exc}\n{tb}"
    )


def guard_call(
    subsystem: str,
    fn: Callable[..., T],
    /,
    *args: Any,
    epic: str = "",
    default: T | None = None,
    **kwargs: Any,
) -> T | None:
    """Invoke *fn*; on failure log full traceback and return *default*."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        log_guarded_exception(subsystem, exc, epic=epic)
        return default
