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
    try:
        from trading.micro_lot_verification import (
            micro_contract_size,
            micro_lot_verification_enabled,
        )

        if micro_lot_verification_enabled():
            return micro_contract_size()
    except Exception:
        pass
    return float(EPIC_OPERATIONAL_SIZE_FLOORS.get(str(epic or "").strip(), 0.0))


def apply_operational_size_floor(size: float, epic: str) -> float:
    """Raise size to epic-class floor, then weld to IG two-decimal lot contract."""
    from trading.position_ladder import apply_broker_lot_contract

    return apply_broker_lot_contract(size, epic)
