"""
Virtual clock for regional lifecycle contract validation.

Advances simulated UK time through session boundaries without touching
the host system clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")


@dataclass
class VirtualClock:
    """Deterministic UK-time controller for stress/regression suites."""

    start: datetime
    step: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    _cursor: datetime | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            self._cursor = self.start.replace(tzinfo=_LONDON)
        else:
            self._cursor = self.start.astimezone(_LONDON)

    @property
    def now(self) -> datetime:
        assert self._cursor is not None
        return self._cursor

    def advance(self, *, steps: int = 1) -> datetime:
        assert self._cursor is not None
        self._cursor = self._cursor + (self.step * steps)
        return self._cursor

    def jump_to(self, hour: int, minute: int = 0) -> datetime:
        assert self._cursor is not None
        self._cursor = self._cursor.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return self._cursor

    def walk_until(self, end: datetime) -> Iterator[datetime]:
        """Yield `now` at each step until *end* (inclusive)."""
        assert self._cursor is not None
        target = end if end.tzinfo else end.replace(tzinfo=_LONDON)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_LONDON)
        else:
            target = target.astimezone(_LONDON)
        while self._cursor <= target:
            yield self._cursor
            self._cursor = self._cursor + self.step
