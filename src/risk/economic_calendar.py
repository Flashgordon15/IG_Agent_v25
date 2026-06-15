"""
Config-driven economic calendar blackout — no external API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from system.config import Config
from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class CalendarEvent:
    date: str
    time_bst: str
    description: str
    impact: str
    instruments: list[str]

    def event_at(self) -> datetime | None:
        try:
            hour, minute = (int(x) for x in str(self.time_bst).split(":", 1))
            y, m, d = (int(x) for x in str(self.date).split("-"))
            return datetime(y, m, d, hour, minute, tzinfo=_LONDON)
        except Exception:
            return None


class EconomicCalendar:
    """Loaded once at boot from config — read-only checks."""

    def __init__(self, cfg: Config | None = None) -> None:
        from system.config_loader import get_config

        active = cfg or get_config()
        raw = active.get("economic_calendar")
        block: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        self._enabled = bool(block.get("enabled", True))
        self._pre_min = int(block.get("pre_event_blackout_minutes", 15))
        self._post_min = int(block.get("post_event_blackout_minutes", 30))
        self._events: list[CalendarEvent] = []
        for row in block.get("events") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("impact", "HIGH")).upper() != "HIGH":
                continue
            instruments = row.get("instruments") or ["all"]
            if isinstance(instruments, str):
                instruments = [instruments]
            self._events.append(
                CalendarEvent(
                    date=str(row.get("date") or ""),
                    time_bst=str(row.get("time_bst") or ""),
                    description=str(row.get("description") or "event"),
                    impact=str(row.get("impact") or "HIGH"),
                    instruments=[str(x) for x in instruments],
                )
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def events(self) -> list[CalendarEvent]:
        return list(self._events)

    def _applies(self, epic: str, instruments: list[str]) -> bool:
        if not instruments or "all" in {i.lower() for i in instruments}:
            return True
        return epic in instruments

    def check_block(
        self,
        epic: str,
        *,
        market: str | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Return (blocked, reason)."""
        if not self._enabled:
            return False, ""
        at = now or datetime.now(_LONDON)
        if at.tzinfo is None:
            at = at.replace(tzinfo=_LONDON)
        else:
            at = at.astimezone(_LONDON)
        label = str(market or epic)
        pre = timedelta(minutes=self._pre_min)
        post = timedelta(minutes=self._post_min)
        for ev in self._events:
            if not self._applies(epic, ev.instruments):
                continue
            ev_at = ev.event_at()
            if ev_at is None:
                continue
            window_start = ev_at - pre
            window_end = ev_at + post
            if not (window_start <= at <= window_end):
                continue
            if at < ev_at:
                mins = max(1, int((ev_at - at).total_seconds() // 60) + 1)
                reason = f"{ev.description} in {mins} minutes"
                log_engine(
                    f"[CALENDAR BLOCK] {label} entry suppressed — {reason}"
                )
            else:
                mins = max(0, int((at - ev_at).total_seconds() // 60))
                reason = f"{mins} minutes post {ev.description}"
                log_engine(
                    f"[CALENDAR BLOCK] {label} entry suppressed — {reason}"
                )
            return True, reason
        return False, ""

    def active_blackouts(
        self,
        epic: str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Dashboard feed — active blackout windows for epic."""
        if not self._enabled:
            return []
        at = now or datetime.now(_LONDON)
        if at.tzinfo is None:
            at = at.replace(tzinfo=_LONDON)
        else:
            at = at.astimezone(_LONDON)
        out: list[dict[str, Any]] = []
        pre = timedelta(minutes=self._pre_min)
        post = timedelta(minutes=self._post_min)
        for ev in self._events:
            if not self._applies(epic, ev.instruments):
                continue
            ev_at = ev.event_at()
            if ev_at is None:
                continue
            if (ev_at - pre) <= at <= (ev_at + post):
                out.append(
                    {
                        "description": ev.description,
                        "event_at": ev_at.isoformat(),
                        "phase": "pre" if at < ev_at else "post",
                    }
                )
        return out


_CALENDAR: EconomicCalendar | None = None


def get_economic_calendar(cfg: Config | None = None) -> EconomicCalendar:
    global _CALENDAR
    if _CALENDAR is None or cfg is not None:
        _CALENDAR = EconomicCalendar(cfg)
    return _CALENDAR


def reset_economic_calendar_for_tests() -> None:
    global _CALENDAR
    _CALENDAR = None


def check_economic_calendar_block(
    epic: str,
    cfg: Config,
    *,
    market: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    return get_economic_calendar(cfg).check_block(
        epic, market=market, now=now
    )
