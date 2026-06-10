"""Operational contract floors — prevent stacked decay below tradeable IG sizes."""

from __future__ import annotations

# Minimum deal size by epic class (IG contracts / £-per-point units).
EPIC_OPERATIONAL_SIZE_FLOORS: dict[str, float] = {
    "IX.D.DOW.IFM.IP": 0.20,
    "IX.D.NASDAQ.IFM.IP": 0.20,
    "CS.D.GBPUSD.CFD.IP": 2.0,
    "CS.D.CFPGOLD.CFP.IP": 1.0,
}


def operational_size_floor(epic: str) -> float:
    """Return configured floor for epic, or 0 when no class floor applies."""
    return float(EPIC_OPERATIONAL_SIZE_FLOORS.get(str(epic or "").strip(), 0.0))


def apply_operational_size_floor(size: float, epic: str) -> float:
    """Raise size to the epic-class operational minimum when configured."""
    floor = operational_size_floor(epic)
    if floor <= 0:
        return float(size)
    return max(float(size), floor)
