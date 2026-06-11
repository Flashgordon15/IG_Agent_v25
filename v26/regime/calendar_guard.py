"""High-impact calendar blackout for v26 shadow intents."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from strategies.base import ShadowIntent


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
    path = _project_root() / "config" / "calendar.json"
    if not path.is_file():
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"block_minutes_before": 30, "block_minutes_after": 30, "events": []}


def _finnhub_events() -> list[dict[str, Any]]:
    path = _project_root() / "data_lake" / "external" / "finnhub_economic_calendar.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events") if isinstance(data, dict) else []
        return events if isinstance(events, list) else []
    except Exception:
        return []


def _all_timed_events() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cfg = _calendar_config()
    for ev in cfg.get("events") or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("time"):
            rows.append(ev)
    for ev in _finnhub_events():
        if isinstance(ev, dict) and ev.get("time"):
            rows.append(ev)
    return rows


def epic_in_event_markets(epic: str, markets: Any) -> bool:
    if not markets:
        return True
    if isinstance(markets, str):
        return epic == markets or not markets
    if isinstance(markets, list):
        return epic in markets or len(markets) == 0
    return True


def is_news_blocked(epic: str, ts: str) -> tuple[bool, str]:
    """Return (blocked, reason) for epic at event timestamp."""
    dt = _parse_ts(ts)
    if dt is None:
        return False, ""
    cfg = _calendar_config()
    before = int(cfg.get("block_minutes_before") or 30)
    after = int(cfg.get("block_minutes_after") or 30)
    window_before = timedelta(minutes=before)
    window_after = timedelta(minutes=after)

    for ev in _all_timed_events():
        impact = str(ev.get("impact") or "").lower()
        if impact and impact not in ("high", "3"):
            continue
        ev_dt = _parse_ts(str(ev.get("time") or ""))
        if ev_dt is None:
            continue
        if not epic_in_event_markets(epic, ev.get("markets")):
            continue
        start = ev_dt - window_before
        end = ev_dt + window_after
        if start <= dt <= end:
            title = str(ev.get("title") or ev.get("event") or "high-impact event")
            return True, f"calendar block ±{before}m: {title[:80]}"
    return False, ""


def apply_calendar_guard(intent: ShadowIntent, event: dict[str, Any]) -> ShadowIntent:
    if not intent.would_trade:
        return intent
    blocked, reason = is_news_blocked(
        intent.epic, intent.source_ts or str(event.get("ts") or "")
    )
    if not blocked:
        return intent
    intent.would_trade = False
    intent.reason = f"{intent.reason} | {reason}".strip(" |")
    intent.payload = {
        **intent.payload,
        "calendar_blocked": True,
        "calendar_reason": reason,
    }
    return intent


def reset_calendar_guard_cache_for_tests() -> None:
    _calendar_config.cache_clear()
