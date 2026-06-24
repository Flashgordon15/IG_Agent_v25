"""
Premium overnight execution — v29.1 permanent night-matrix lockdown.

OPERATIONAL LOCKDOWN (do not revert):
- Legacy weekday blackout 20:00–06:00 BST is DELETED for the night matrix.
- Only the 7-minute institutional day-rollover lock (21:58–22:05 BST) may block entries.
- Gold, Wall Street, Japan 225, EUR/USD: 24/7 clearance when microstructure fires.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")

# --- v29.1 permanent night-matrix constants (hardcoded; config mirrors only) ---
NIGHT_MATRIX_LOCKDOWN_VERSION = "v29.1"
LEGACY_WEEKDAY_BLACKOUT_DELETED = True
ROLLOVER_LOCK_START_BST = "21:58"
ROLLOVER_LOCK_END_BST = "22:05"
LEGACY_BLACKOUT_REMOVED = "20:00-06:00 BST weekday curfew — superseded"

NIGHT_MATRIX_EPICS = frozenset(
    {
        "CS.D.CFPGOLD.CFP.IP",  # Gold
        "IX.D.DOW.IFM.IP",  # Wall Street
        "IX.D.NIKKEI.IFM.IP",  # Japan 225
        "CS.D.EURUSD.CFD.IP",  # EUR/USD
        "CS.D.CRUDE.CFD.IP",  # Brent / WTI CFD
        "IX.D.FTSE.IFM.IP",  # FTSE 100
        "IX.D.DAX.IFM.IP",  # DAX 40
    }
)

DEFAULT_PREMIUM_EPICS = tuple(NIGHT_MATRIX_EPICS)

OVERNIGHT_MOMENTUM_REGIMES = frozenset(
    {
        "MOMENTUM_UP",
        "MOMENTUM_DOWN",
        "SWEEP_BUY",
        "SWEEP_SELL",
    }
)


def _premium_cfg(config: Any | None) -> dict[str, Any]:
    if config is None:
        return {}
    try:
        ep = config.get("entry_protection") or config.entry_protection
    except Exception:
        ep = getattr(config, "entry_protection", {})
    if not isinstance(ep, dict):
        return {}
    block = ep.get("premium_overnight")
    return dict(block) if isinstance(block, dict) else {}


def premium_overnight_enabled(config: Any | None) -> bool:
    """
    Permanent lockdown: always True unless explicit test-only override in config.
    """
    block = _premium_cfg(config)
    if block.get("lockdown_override_disable") is True:
        return bool(block.get("enabled", False))
    if block.get("lockdown_permanent") is False and block.get("enabled") is False:
        return False
    return True


def is_night_matrix_epic(epic: str) -> bool:
    return str(epic or "").strip() in NIGHT_MATRIX_EPICS


def premium_overnight_epics(config: Any | None) -> frozenset[str]:
    block = _premium_cfg(config)
    raw = block.get("epics") or DEFAULT_PREMIUM_EPICS
    merged = frozenset(str(e).strip() for e in raw if str(e).strip())
    return merged | NIGHT_MATRIX_EPICS


def is_premium_overnight_epic(epic: str, config: Any | None) -> bool:
    if not premium_overnight_enabled(config):
        return False
    return is_night_matrix_epic(epic)


def _london_now(now: datetime | None = None) -> datetime:
    at = now or datetime.now(_LONDON)
    if at.tzinfo is None:
        return at.replace(tzinfo=_LONDON)
    return at.astimezone(_LONDON)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value or "00:00").strip().split(":")
    return int(parts[0]), int(parts[1])


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _in_same_day_window(dt: datetime, start: str, end: str) -> bool:
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    t = _minutes_since_midnight(dt)
    return start_m <= t < end_m


def rollover_lock_window(config: Any | None = None) -> tuple[str, str]:
    block = _premium_cfg(config)
    start = str(block.get("rollover_lock_start", ROLLOVER_LOCK_START_BST))
    end = str(block.get("rollover_lock_end", ROLLOVER_LOCK_END_BST))
    return start, end


def in_rollover_lock(
    *,
    now: datetime | None = None,
    config: Any | None = None,
) -> bool:
    """7-minute institutional day-clear lock — the ONLY scheduled session block for night matrix."""
    start, end = rollover_lock_window(config)
    return _in_same_day_window(_london_now(now), start, end)


def in_overnight_liquidity_window(
    *,
    now: datetime | None = None,
    config: Any | None = None,
) -> bool:
    """22:00–06:00 BST overlap window (used for telemetry labelling; not a hard block)."""
    block = _premium_cfg(config)
    start = str(block.get("overnight_session_start", "22:00"))
    end = str(block.get("overnight_session_end", "06:00"))
    at = _london_now(now)
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    t = _minutes_since_midnight(at)
    if start_m <= end_m:
        return start_m <= t < end_m
    return t >= start_m or t < end_m


def night_matrix_session_allowed(
    epic: str,
    *,
    config: Any | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    Authoritative session clearance for night matrix epics.
    Returns (allowed, reason_if_blocked).
    """
    if not is_premium_overnight_epic(epic, config):
        return True, ""
    if in_rollover_lock(now=now, config=config):
        rs, re = rollover_lock_window(config)
        return False, f"rollover lock {rs}-{re} BST"
    return True, ""


def overnight_momentum_confidence_floor(config: Any | None) -> float:
    block = _premium_cfg(config)
    try:
        return float(block.get("momentum_confidence_floor", 0.72))
    except (TypeError, ValueError):
        return 0.72


def overnight_signal_confidence_relief(config: Any | None) -> float:
    block = _premium_cfg(config)
    try:
        return float(block.get("signal_confidence_relief_pts", 8.0))
    except (TypeError, ValueError):
        return 8.0


def premium_overnight_momentum_pass(
    epic: str,
    regime: str,
    confidence: float,
    *,
    config: Any | None = None,
    now: datetime | None = None,
) -> bool:
    """24/7 premium microstructure clearance (blocked only during rollover lock)."""
    if not is_premium_overnight_epic(epic, config):
        return False
    allowed, _ = night_matrix_session_allowed(epic, config=config, now=now)
    if not allowed:
        return False
    if str(regime or "") not in OVERNIGHT_MOMENTUM_REGIMES:
        return False
    return float(confidence or 0.0) >= overnight_momentum_confidence_floor(config)
