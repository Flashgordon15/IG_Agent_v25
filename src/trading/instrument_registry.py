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
        return [inst for _iid, inst in self.get_enabled_with_ids()]

    def get_enabled_with_ids(self) -> list[tuple[str, dict[str, Any]]]:
        """Enabled instruments as (instrument_id, config dict), highest priority first."""
        out: list[tuple[str, dict[str, Any]]] = []
        for iid, inst in self._instruments.items():
            if bool(inst.get("enabled")):
                row = deepcopy(inst)
                row.setdefault("instrument_id", iid)
                out.append((iid, row))
        out.sort(
            key=lambda pair: int(pair[1].get("execution_priority") or 0),
            reverse=True,
        )
        return out

    def get_by_id(self, instrument_id: str) -> dict[str, Any] | None:
        key = str(instrument_id or "").strip()
        if not key or key not in self._instruments:
            return None
        row = deepcopy(self._instruments[key])
        row.setdefault("instrument_id", key)
        return row

    def get_by_epic(self, epic: str) -> dict[str, Any] | None:
        epic_key = str(epic or "").strip()
        if not epic_key:
            return None
        for iid, inst in self._instruments.items():
            if str(inst.get("epic") or "").strip() == epic_key:
                row = deepcopy(inst)
                row.setdefault("instrument_id", iid)
                return row
        return None

    def session_whitelist_for_epic(self, epic: str) -> list[str]:
        inst = self.get_by_epic(epic)
        if not inst:
            return []
        wl = inst.get("trading_session_whitelist")
        if isinstance(wl, list) and wl:
            return [str(s) for s in wl]
        return []
