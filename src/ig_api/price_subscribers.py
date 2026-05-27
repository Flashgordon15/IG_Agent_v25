"""Multiplex price/state callbacks — bot and UI can subscribe without overwriting."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class CallbackList(Generic[T]):
    def __init__(self) -> None:
        self._callbacks: list[Callable[[T], None]] = []

    def subscribe(self, callback: Callable[[T], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def clear(self) -> None:
        self._callbacks.clear()

    def emit(self, value: T) -> None:
        for cb in list(self._callbacks):
            try:
                cb(value)
            except Exception:
                pass

    def set_single(self, callback: Callable[[T], None] | None) -> None:
        """Replace all subscribers with one (legacy behaviour)."""
        self._callbacks.clear()
        if callback:
            self._callbacks.append(callback)
