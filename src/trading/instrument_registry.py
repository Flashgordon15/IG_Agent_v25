"""
Multi-instrument config registry — Section 4.5 Step 11.

Reads the instruments block from config at init. No trading logic or subscriptions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class InstrumentRegistry:
    """Instrument definitions from config — enabled flags only gate runtime use (later steps)."""

    def __init__(self, config: dict[str, Any]) -> None:
        raw = config.get("instruments")
        if not isinstance(raw, dict):
            self._instruments: dict[str, dict[str, Any]] = {}
        else:
            self._instruments = {
                str(key): dict(value)
                for key, value in raw.items()
                if isinstance(value, dict)
            }

    def get_all(self) -> list[dict[str, Any]]:
        """All instrument config dicts (enabled and disabled)."""
        return [deepcopy(inst) for inst in self._instruments.values()]

    def get_enabled(self) -> list[dict[str, Any]]:
        """Instruments with enabled=true only."""
        return [
            deepcopy(inst)
            for inst in self._instruments.values()
            if bool(inst.get("enabled"))
        ]

    def get_by_epic(self, epic: str) -> dict[str, Any] | None:
        epic_key = str(epic or "").strip()
        if not epic_key:
            return None
        for inst in self._instruments.values():
            if str(inst.get("epic") or "").strip() == epic_key:
                return deepcopy(inst)
        return None
