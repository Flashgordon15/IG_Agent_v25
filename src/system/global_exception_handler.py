"""Always-on uncaught exception logging — safe before KernelInterceptor arms."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from typing import Any, Callable

_installed = False
_install_lock = threading.Lock()
_original_excepthook: Callable[..., Any] = sys.excepthook
_original_thread_excepthook: Callable[..., Any] | None = getattr(
    threading, "excepthook", None
)


def _format_uncaught(
    subsystem: str,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
) -> str:
    lines = traceback.format_exception(exc_type, exc, tb)
    body = "".join(lines).rstrip()
    return (
        f"uncaught:{subsystem} {exc_type.__name__}: {exc}\n{body}"
        if body
        else f"uncaught:{subsystem} {exc_type.__name__}: {exc}"
    )


def _log_uncaught(
    subsystem: str,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
) -> None:
    if exc_type is KeyboardInterrupt:
        return
    try:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception(
            subsystem,
            exc,
            detail=f"{exc_type.__name__} uncaught",
        )
    except Exception:
        try:
            from system.engine_log import log_engine

            log_engine(_format_uncaught(subsystem, exc_type, exc, tb))
        except Exception:
            pass
    try:
        from system.engine_log import record_engine_warning

        record_engine_warning(
            "uncaught_exception",
            f"{subsystem}: {exc_type.__name__}: {exc}",
        )
    except Exception:
        pass


def _global_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
) -> None:
    if exc_type is KeyboardInterrupt:
        _original_excepthook(exc_type, exc, tb)
        return
    _log_uncaught("main_thread", exc_type, exc, tb)
    hook = getattr(sys, "__excepthook__", None)
    if hook is not None and hook is not _global_excepthook:
        hook(exc_type, exc, tb)
        return
    _original_excepthook(exc_type, exc, tb)


def _global_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    exc_type = args.exc_type
    if exc_type is KeyboardInterrupt:
        if _original_thread_excepthook is not None:
            _original_thread_excepthook(args)
        return
    thread_name = getattr(args.thread, "name", "unknown") if args.thread else "unknown"
    _log_uncaught(f"thread:{thread_name}", exc_type, args.exc_value, args.exc_traceback)
    hook = getattr(threading, "excepthook", None)
    if hook is not None and hook is not _global_thread_excepthook:
        hook(args)
        return
    if _original_thread_excepthook is not None:
        _original_thread_excepthook(args)


def install_global_exception_handlers(*, force: bool = False) -> bool:
    """Idempotent install — logs only; never hard-exits (trading-safe)."""
    global _installed
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1" and not force:
        return False
    with _install_lock:
        if _installed and not force:
            return False
        sys.excepthook = _global_excepthook
        if hasattr(threading, "excepthook"):
            threading.excepthook = _global_thread_excepthook
        _installed = True
        try:
            from system.engine_log import log_engine

            log_engine("global_exception_handler: sys.excepthook + threading.excepthook armed")
        except Exception:
            pass
        return True


def reset_global_exception_handlers_for_tests() -> None:
    global _installed
    with _install_lock:
        sys.excepthook = _original_excepthook
        if hasattr(threading, "excepthook") and _original_thread_excepthook is not None:
            threading.excepthook = _original_thread_excepthook
        _installed = False
