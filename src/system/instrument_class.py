"""
Instrument classification — weekend vs weekday CFD contracts.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_UK = ZoneInfo("Europe/London")


def is_weekend_epic(epic: str, market_name: str = "") -> bool:
    """True for IG weekend index/commodity contract epics."""
    blob = f"{epic} {market_name}".upper()
    return "WEEKEND" in blob or ".WEEK." in blob


def is_weekend_market_hours(now: datetime | None = None) -> bool:
    """
    IG weekend index window (UK): Saturday 08:00 → Sunday ~22:00.

    Weekday cash/index epics should not rotate or trail on weekend feeds outside
    this window.
    """
    now = now or datetime.now(_UK)
    wd = now.weekday()
    if wd == 5 and now.hour >= 8:
        return True
    if wd == 6 and now.hour < 22:
        return True
    return False


def filter_rotation_epics(
    epics: list[str],
    *,
    market_names: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """
    Drop weekend epics during weekday sessions and weekday epics on Saturday.

    Prevents weekday trailing/rotation logic from binding to weekend contract feeds.
    """
    now = now or datetime.now(_UK)
    names = market_names or {}
    weekend_hours = is_weekend_market_hours(now)
    wd = now.weekday()
    out: list[str] = []
    for epic in epics:
        label = names.get(epic, "")
        if is_weekend_epic(epic, label):
            if weekend_hours:
                out.append(epic)
            continue
        if wd == 5:
            continue
        out.append(epic)
    return out if out else list(epics)
