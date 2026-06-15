"""Agent clock payload for dashboard BST display (read-only)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")
_UTC = ZoneInfo("UTC")
_BOUNDARY_MINUTES = 30.0


def _enabled_epics() -> list[str]:
    try:
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        cfg = get_config()
        epics: list[str] = []
        for _iid, inst in InstrumentRegistry(cfg.as_dict()).get_enabled_with_ids():
            epic = str(inst.get("epic") or "").strip()
            if epic:
                epics.append(epic)
        return epics
    except Exception:
        return []


def _minutes_to_hhmm_boundary(at: datetime, hour: int, minute: int) -> float:
    """Minutes until the next occurrence of hour:minute Europe/London."""
    local = at.astimezone(_LONDON)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds() / 60.0


def _gold_blackout_boundary_minutes(at: datetime, epic: str, cfg: Any) -> list[float]:
    """Display-only: minutes to gold session-blackout window edges."""
    from trading.entry_protection import _session_rules_for_epic, _parse_hhmm

    rules = _session_rules_for_epic(epic, cfg)
    if rules is None:
        return []
    out: list[float] = []
    try:
        sh, sm = _parse_hhmm(rules["weekday_start"])
        eh, em = _parse_hhmm(rules["weekday_end"])
        out.append(_minutes_to_hhmm_boundary(at, sh, sm))
        out.append(_minutes_to_hhmm_boundary(at, eh, em))
    except Exception:
        pass
    return out


def _calendar_boundary_minutes(epic: str, at: datetime) -> list[float]:
    """Minutes to next IG calendar open/close for an epic."""
    from system.market_watch.calendar import get_market_status, is_market_open

    out: list[float] = []
    try:
        if is_market_open(epic, at=at):
            from system.market_watch.calendar import minutes_until_market_close

            mins = minutes_until_market_close(epic, at=at)
            if mins is not None:
                out.append(float(mins))
        else:
            status = get_market_status(epic, at=at)
            if status and status.next_open_at:
                delta = status.next_open_at - at.astimezone(
                    ZoneInfo(status.timezone or "Europe/London")
                )
                mins = delta.total_seconds() / 60.0
                if mins > 0:
                    out.append(mins)
    except Exception:
        pass
    return out


def _resolve_clock_status(at: datetime) -> str:
    """
    green — at least one enabled instrument in a tradable window.
    amber — within 30 minutes of a session/blackout boundary.
    red   — all enabled instruments calendar-closed or session-blackout blocked.
    """
    from system.config_loader import get_config
    from system.market_watch.calendar import is_market_open
    from trading.entry_protection import check_session_blackout

    cfg = get_config()
    epics = _enabled_epics()
    if not epics:
        return "green"

    tradable: list[bool] = []
    boundary_hits: list[bool] = []

    for epic in epics:
        try:
            cal_open = bool(is_market_open(epic, at=at))
            blackout, _ = check_session_blackout(epic, cfg, now=at)
            tradable.append(cal_open and not blackout)
        except Exception:
            tradable.append(False)

        mins_candidates = _calendar_boundary_minutes(epic, at)
        mins_candidates.extend(_gold_blackout_boundary_minutes(at, epic, cfg))
        boundary_hits.append(
            any(0 < m <= _BOUNDARY_MINUTES for m in mins_candidates)
        )

    if not any(tradable):
        return "red"
    if any(boundary_hits):
        return "amber"
    return "green"


def _next_market_boundary(at: datetime) -> dict[str, Any]:
    """Nearest IG calendar open/close across enabled epics (display only)."""
    from system.market_watch.calendar import get_market_status, is_market_open

    epics = _enabled_epics()
    best: dict[str, Any] | None = None
    local = at.astimezone(_LONDON)

    for epic in epics:
        try:
            if is_market_open(epic, at=at):
                from system.market_watch.calendar import minutes_until_market_close

                mins = minutes_until_market_close(epic, at=at)
                if mins is None or mins <= 0:
                    continue
                candidate = {
                    "minutes_to_boundary": int(round(float(mins))),
                    "boundary_type": "CLOSE",
                    "epic": epic,
                }
            else:
                status = get_market_status(epic, at=at)
                if not status or not status.next_open_at:
                    continue
                tz = ZoneInfo(status.timezone or "Europe/London")
                delta = status.next_open_at - at.astimezone(tz)
                mins = delta.total_seconds() / 60.0
                if mins <= 0:
                    continue
                candidate = {
                    "minutes_to_boundary": int(round(mins)),
                    "boundary_type": "OPEN",
                    "epic": epic,
                }
        except Exception:
            continue

        if best is None or candidate["minutes_to_boundary"] < best["minutes_to_boundary"]:
            best = candidate

    if best is None:
        return {
            "next_boundary": None,
            "minutes_to_boundary": None,
            "boundary_type": None,
        }

    target = local + timedelta(minutes=best["minutes_to_boundary"])
    return {
        "next_boundary": target.strftime("%H:%M"),
        "minutes_to_boundary": best["minutes_to_boundary"],
        "boundary_type": best["boundary_type"],
    }


def get_agent_time_payload(*, at: datetime | None = None) -> dict[str, Any]:
    """BST/UTC clock fields for GET /api/time."""
    from signals.indicators import session_name

    now_utc = (at or datetime.now(_UTC)).astimezone(_UTC)
    now_bst = now_utc.astimezone(_LONDON)
    boundary = _next_market_boundary(now_bst)

    return {
        "bst": now_bst.strftime("%H:%M:%S"),
        "utc": now_utc.strftime("%H:%M:%S"),
        "weekday": now_bst.strftime("%A"),
        "date": now_bst.strftime("%d %b %Y"),
        "session": session_name(now_bst),
        "clock_status": _resolve_clock_status(now_bst),
        "utc_epoch": now_utc.timestamp(),
        **boundary,
    }
