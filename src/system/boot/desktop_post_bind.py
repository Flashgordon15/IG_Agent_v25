"""Post-bind desktop/launcher boot hooks — registered by main.py at import."""

from __future__ import annotations

import threading
from typing import Any, Callable

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_RUNNER: Callable[..., None] | None = None
_THREAD: threading.Thread | None = None


def register_post_bind_runner(fn: Callable[..., None]) -> None:
    global _RUNNER
    _RUNNER = fn


def run_deferred_desktop_boot(*, boot_context: Any | None = None) -> None:
    if _RUNNER is None:
        log_engine("desktop_post_bind: no runner registered — skip")
        return
    _RUNNER(boot_context=boot_context)


def schedule_desktop_post_bind(*, boot_context: Any | None = None) -> None:
    """Run deferred launcher/desktop chores off the API bind critical path."""

    def _work() -> None:
        try:
            run_deferred_desktop_boot(boot_context=boot_context)
        except Exception as exc:
            log_guarded_exception("desktop_post_bind", exc)

    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        chained = _THREAD

        def _chain() -> None:
            chained.join(timeout=120.0)
            _work()

        threading.Thread(
            target=_chain,
            name="desktop-post-bind-chain",
            daemon=True,
        ).start()
        return
    _THREAD = threading.Thread(
        target=_work,
        name="desktop-post-bind",
        daemon=True,
    )
    _THREAD.start()
