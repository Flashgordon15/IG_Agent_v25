"""
Overnight / rollover financing estimates for open-position unrealized P&L.

IG rolls CFD positions at ~22:00 UK. Trailing-stop logic should see fee erosion
on multi-day holds even when mark-to-market quotes are flat.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_UK = ZoneInfo("Europe/London")
_ROLLOVER_HOUR = 22

# Daily financing approximations (account GBP per unit size per day) — conservative.
_DEFAULT_INDEX_DAILY_GBP = 0.15
_DEFAULT_FX_DAILY_GBP = 0.08


def _parse_opened_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text[:19], fmt)
            return dt.replace(tzinfo=_UK)
        except ValueError:
            continue
    try:
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=_UK)
    except (TypeError, ValueError):
        return None


def rollover_count_since(opened: datetime, *, now: datetime | None = None) -> int:
    """Count 22:00 UK rollovers strictly after open and up to now."""
    now = now or datetime.now(_UK)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=_UK)
    count = 0
    day = opened.date()
    end_day = now.date()
    while day <= end_day:
        roll = datetime(day.year, day.month, day.day, _ROLLOVER_HOUR, 0, 0, tzinfo=_UK)
        if roll > opened and roll <= now:
            count += 1
        day += timedelta(days=1)
    return count


def _daily_funding_gbp_per_unit(epic: str) -> float:
    from system.pnl_math import pip_size_for_epic

    if pip_size_for_epic(epic) is not None:
        return _DEFAULT_FX_DAILY_GBP
    return _DEFAULT_INDEX_DAILY_GBP


def accrued_funding_gbp_for_position(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float:
    """
    Estimated cumulative overnight financing (GBP) since open.

    Deduct from unrealized P&L so protection logic sees net carry cost.
    """
    opened = _parse_opened_at(row.get("opened_at") or row.get("open_time"))
    if opened is None:
        return 0.0
    try:
        size = max(0.0, float(row.get("size") or 0))
    except (TypeError, ValueError):
        return 0.0
    if size <= 0:
        return 0.0
    epic = str(row.get("epic") or "")
    rolls = rollover_count_since(opened, now=now)
    if rolls <= 0:
        return 0.0
    per_unit = _daily_funding_gbp_per_unit(epic)
    return round(rolls * per_unit * size, 4)
