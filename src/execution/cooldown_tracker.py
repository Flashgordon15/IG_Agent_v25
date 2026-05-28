"""Per-epic trade cooldown tracking — persists to LearningStore across restarts."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from data.learning_store import LearningStore


def cooldown_key(epic: str, direction: str | None = None) -> str:
    """Per epic:direction cooldown — BUY must not block SELL."""
    ep = str(epic or "").strip()
    if not direction:
        return ep
    return f"{ep}:{str(direction).upper()}"


class CooldownTracker:
    def __init__(self, cooldown_seconds: int, store: Any | None = None) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_trade: dict[str, datetime] = {}
        self._store: LearningStore | None = store

    def attach_store(self, store: Any) -> None:
        """Attach the LearningStore and restore any unexpired cooldowns from disk."""
        self._store = store
        self._restore_from_store()

    def _restore_from_store(self) -> None:
        if self._store is None:
            return
        try:
            active = self._store.load_active_cooldowns()
            for epic, recorded_at in active.items():
                if epic not in self._last_trade:
                    self._last_trade[epic] = recorded_at
        except Exception:
            pass

    def record(
        self,
        epic: str,
        when: datetime | None = None,
        *,
        direction: str | None = None,
    ) -> None:
        key = cooldown_key(epic, direction)
        self._last_trade[key] = when or datetime.now()
        if self._store is not None:
            try:
                self._store.record_cooldown(key, self.cooldown_seconds)
            except Exception:
                pass

    def remaining_seconds(self, epic: str, direction: str | None = None) -> int:
        key = cooldown_key(epic, direction)
        last = self._last_trade.get(key)
        if not last:
            return 0
        elapsed = (datetime.now() - last).total_seconds()
        return max(0, int(self.cooldown_seconds - elapsed))

    def is_active(self, epic: str, direction: str | None = None) -> bool:
        return self.remaining_seconds(epic, direction) > 0

    def format_remaining(self, epic: str, direction: str | None = None) -> str:
        secs = self.remaining_seconds(epic, direction)
        if secs <= 0:
            return "READY"
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if m else f"{s}s"
