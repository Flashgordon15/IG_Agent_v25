"""Holds the uvicorn asyncio loop for cross-thread Gate 5 lazy warmup scheduling."""

from __future__ import annotations

import asyncio
from typing import Optional

_loop: Optional[asyncio.AbstractEventLoop] = None


def set_boot_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def get_boot_loop() -> asyncio.AbstractEventLoop | None:
    return _loop


def schedule_coro(coro) -> None:
    """Schedule coroutine on boot loop from a worker thread (uses create_task)."""
    loop = get_boot_loop()
    if loop is None or not loop.is_running():
        raise RuntimeError("boot event loop not registered")
    loop.call_soon_threadsafe(lambda: asyncio.create_task(coro))
