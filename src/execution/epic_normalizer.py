"""Night-matrix epic canonicalization — unified CS.D.CFPGOLD / IX.D.DOW keys."""

from __future__ import annotations

GOLD_CANONICAL = "CS.D.CFPGOLD.CFP.IP"
WALL_CANONICAL = "IX.D.DOW.IFM.IP"

# Legacy / typo variants → canonical night-matrix epics.
_EPIC_ALIAS_MAP: dict[str, str] = {
    "CC.D.CFPGOLD.CFP.IP": GOLD_CANONICAL,
    "CS.D.GOLD.CFD.IP": GOLD_CANONICAL,
    "CS.D.CFPGOLD.IP": GOLD_CANONICAL,
    "CFPGOLD": GOLD_CANONICAL,
    "GOLD": GOLD_CANONICAL,
    "GC=F": GOLD_CANONICAL,
    "IX.D.DOW.IDF.IP": WALL_CANONICAL,
    "IX.D.DOW.IP": WALL_CANONICAL,
    "IX.D.WALLST.IFM.IP": WALL_CANONICAL,
    "WALLST": WALL_CANONICAL,
    "DOW": WALL_CANONICAL,
    "^DJI": WALL_CANONICAL,
}


def normalize_night_matrix_epic(epic: str) -> str:
    """Map Gold / Wall St (and legacy strings) to active 24/7 night-matrix epics."""
    key = str(epic or "").strip()
    if not key:
        return key
    upper = key.upper()
    if upper in _EPIC_ALIAS_MAP:
        return _EPIC_ALIAS_MAP[upper]
    if "CFPGOLD" in upper or (upper.endswith(".IP") and "GOLD" in upper and "CFP" in upper):
        return GOLD_CANONICAL
    if "DOW" in upper and ".IP" in upper:
        return WALL_CANONICAL
    if "DOW" in upper or "WALL" in upper:
        return WALL_CANONICAL
    return key
