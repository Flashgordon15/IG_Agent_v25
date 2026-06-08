"""Live calendar blackout — high-impact events ±30m (config + Finnhub)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from system.paths import project_root


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _calendar_config() -> dict[str, Any]:
    path = project_root() / "config" / "calendar.json"
    if not path.is_file():
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}


def _finnhub_events() -> list[dict[str, Any]]:
    path = project_root() / "data_lake" / "external" / "finnhub_economic_calendar.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events") if isinstance(data, dict) else []
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _epic_in_markets(epic: str, markets: Any) -> bool:
    if not markets:
        return True
    if isinstance(markets, str):
        return epic == markets or not markets
    if isinstance(markets, list):
        return epic in markets or len(markets) == 0
    return True


def is_calendar_blocked(
    epic: str,
    *,
    at: datetime | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason) for epic at UTC time."""
    dt = at or datetime.now(timezone.utc)
    cfg = _calendar_config()
    before = int(cfg.get("block_minutes_before") or 30)
    after = int(cfg.get("block_minutes_after") or 30)
    window_before = timedelta(minutes=before)
    window_after = timedelta(minutes=after)

    for ev in _finnhub_events():
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        if (ev_dt - window_before) <= dt <= (ev_dt + window_after):
            title = str(ev.get("title") or "high-impact event")[:80]
            return True, f"calendar ±{before}m: {title}"

    for ev in cfg.get("events") or []:
        if not isinstance(ev, dict):
            continue
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not _epic_in_markets(epic, ev.get("markets")):
            continue
        if (ev_dt - window_before) <= dt <= (ev_dt + window_after):
            title = str(ev.get("title") or "high-impact event")[:80]
            return True, f"calendar ±{before}m: {title}"

    return False, ""


def reset_calendar_gate_cache_for_tests() -> None:
    _calendar_config.cache_clear()
