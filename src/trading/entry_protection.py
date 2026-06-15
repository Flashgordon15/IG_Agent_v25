"""
Entry protection — session blackout, re-entry cooldown, ranging filter, daily cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from system.config import Config
from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _ep_cfg(cfg: Config) -> dict[str, Any]:
    raw = cfg.get("entry_protection")
    return dict(raw) if isinstance(raw, dict) else {}


def _enabled(cfg: Config) -> bool:
    ep = _ep_cfg(cfg)
    return bool(ep.get("enabled", True))


def _parse_hhmm(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time {value!r}")
    return int(parts[0]), int(parts[1])


def _parse_dow_hhmm(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if " " not in text:
        raise ValueError(f"invalid dow time {value!r}")
    dow_s, clock = text.split(None, 1)
    dow = _DOW.get(dow_s[:3].lower())
    if dow is None:
        raise ValueError(f"invalid weekday {dow_s!r}")
    hour, minute = _parse_hhmm(clock)
    return dow, hour, minute


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _in_weekday_overnight_blackout(
    dt: datetime, *, start: str, end: str
) -> bool:
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    t = _minutes_since_midnight(dt)
    wd = dt.weekday()
    if start_m <= end_m:
        return start_m <= t < end_m and wd <= 4
    if t >= start_m and wd <= 4:
        return True
    if t < end_m and 1 <= wd <= 5:
        return True
    return False


def _in_weekend_blackout(dt: datetime, *, start: str, end: str) -> bool:
    start_dow, sh, sm = _parse_dow_hhmm(start)
    end_dow, eh, em = _parse_dow_hhmm(end)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    wd = dt.weekday()
    t = _minutes_since_midnight(dt)
    if wd == start_dow and t >= start_m:
        return True
    if start_dow < wd < end_dow:
        return True
    if wd == end_dow and t < end_m:
        return True
    return False


def _session_rules_for_epic(epic: str, cfg: Config) -> dict[str, str] | None:
    ep = _ep_cfg(cfg)
    if not bool(ep.get("session_blackout_enabled", True)):
        return None
    overrides = ep.get("per_epic")
    if isinstance(overrides, dict):
        row = overrides.get(epic)
        if isinstance(row, dict) and row.get("enabled", True):
            return {
                "weekday_start": str(
                    row.get("weekday_blackout_start")
                    or row.get("gold_weekday_blackout_start")
                    or ep.get("gold_weekday_blackout_start", "20:00")
                ),
                "weekday_end": str(
                    row.get("weekday_blackout_end")
                    or row.get("gold_weekday_blackout_end")
                    or ep.get("gold_weekday_blackout_end", "06:00")
                ),
                "weekend_start": str(
                    row.get("weekend_blackout_start")
                    or row.get("gold_weekend_blackout_start")
                    or ep.get("gold_weekend_blackout_start", "Fri 20:00")
                ),
                "weekend_end": str(
                    row.get("weekend_blackout_end")
                    or row.get("gold_weekend_blackout_end")
                    or ep.get("gold_weekend_blackout_end", "Sun 22:00")
                ),
            }
    gold = str(ep.get("gold_epic", "CS.D.CFPGOLD.CFP.IP"))
    if epic == gold:
        return {
            "weekday_start": str(ep.get("gold_weekday_blackout_start", "20:00")),
            "weekday_end": str(ep.get("gold_weekday_blackout_end", "06:00")),
            "weekend_start": str(ep.get("gold_weekend_blackout_start", "Fri 20:00")),
            "weekend_end": str(ep.get("gold_weekend_blackout_end", "Sun 22:00")),
        }
    default_rules = ep.get("default_session_rules")
    if isinstance(default_rules, dict):
        return {
            "weekday_start": str(default_rules.get("weekday_blackout_start", "20:00")),
            "weekday_end": str(default_rules.get("weekday_blackout_end", "06:00")),
            "weekend_start": str(
                default_rules.get("weekend_blackout_start", "Fri 20:00")
            ),
            "weekend_end": str(default_rules.get("weekend_blackout_end", "Sun 22:00")),
        }
    return None


def resolve_epic_for_market(market: str, cfg: Config) -> str:
    key = str(market or "").strip()
    if not key:
        return str(cfg.epic or "")
    if key.count(".") >= 2:
        return key
    try:
        from trading.instrument_registry import InstrumentRegistry

        for inst in InstrumentRegistry(cfg.as_dict()).get_all():
            epic = str(inst.get("epic") or "").strip()
            name = str(inst.get("name") or "").strip()
            if epic and epic == key:
                return epic
            if name and name.lower() == key.lower() and epic:
                return epic
    except Exception:
        pass
    return str(cfg.epic or key)


def check_session_blackout(
    epic: str,
    cfg: Config,
    now: datetime | None = None,
    *,
    market: str | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason). blocked=True suppresses entry."""
    if not _enabled(cfg):
        return False, ""
    rules = _session_rules_for_epic(str(epic or "").strip(), cfg)
    if rules is None:
        return False, ""
    at = now or datetime.now(_LONDON)
    if at.tzinfo is None:
        at = at.replace(tzinfo=_LONDON)
    else:
        at = at.astimezone(_LONDON)
    weekday_block = _in_weekday_overnight_blackout(
        at,
        start=rules["weekday_start"],
        end=rules["weekday_end"],
    )
    weekend_block = _in_weekend_blackout(
        at,
        start=rules["weekend_start"],
        end=rules["weekend_end"],
    )
    if weekday_block or weekend_block:
        label = str(market or epic)
        parts: list[str] = []
        if weekday_block:
            parts.append(
                f"weekday blackout {rules['weekday_start']}-{rules['weekday_end']} BST"
            )
        if weekend_block:
            parts.append(
                f"weekend blackout {rules['weekend_start']}–{rules['weekend_end']} BST"
            )
        reason = "; ".join(parts)
        log_engine(
            f"[SESSION BLOCK] {label} entry suppressed — outside trading window ({reason})"
        )
        return True, reason
    return False, ""


@dataclass
class EntryProtectionState:
    last_close_time: dict[str, datetime] = field(default_factory=dict)
    last_close_was_loss: dict[str, bool] = field(default_factory=dict)
    daily_opens: dict[str, tuple[date, int]] = field(default_factory=dict)

    def reset(self) -> None:
        self.last_close_time.clear()
        self.last_close_was_loss.clear()
        self.daily_opens.clear()


_STATE = EntryProtectionState()


def get_entry_protection_state() -> EntryProtectionState:
    return _STATE


def reset_entry_protection_state() -> None:
    _STATE.reset()


def record_epic_close(epic: str, pnl_gbp: float | None) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    at = datetime.now(_LONDON)
    _STATE.last_close_time[key] = at
    _STATE.last_close_was_loss[key] = float(pnl_gbp or 0.0) < 0.0


def check_reentry_cooldown(
    epic: str,
    cfg: Config,
    now: datetime | None = None,
    *,
    market: str | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason)."""
    if not _enabled(cfg):
        return False, ""
    ep = _ep_cfg(cfg)
    key = str(epic or "").strip()
    closed_at = _STATE.last_close_time.get(key)
    if closed_at is None:
        return False, ""
    at = now or datetime.now(_LONDON)
    if at.tzinfo is None:
        at = at.replace(tzinfo=_LONDON)
    else:
        at = at.astimezone(_LONDON)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=_LONDON)
    else:
        closed_at = closed_at.astimezone(_LONDON)
    loss = bool(_STATE.last_close_was_loss.get(key))
    minutes = int(
        ep.get("reentry_cooldown_after_loss_minutes", 30)
        if loss
        else ep.get("reentry_cooldown_minutes", 15)
    )
    elapsed = at - closed_at
    remaining = timedelta(minutes=minutes) - elapsed
    if remaining.total_seconds() <= 0:
        return False, ""
    mins_left = max(1, int(remaining.total_seconds() // 60) + 1)
    label = str(market or epic)
    log_engine(
        f"[COOLDOWN] {label} entry suppressed — {mins_left}m remaining after last close"
    )
    return True, f"{mins_left}m remaining after last close"


def _london_today(at: datetime) -> date:
    if at.tzinfo is None:
        at = at.replace(tzinfo=_LONDON)
    return at.astimezone(_LONDON).date()


def increment_daily_trade_count(epic: str, now: datetime | None = None) -> int:
    key = str(epic or "").strip()
    if not key:
        return 0
    at = now or datetime.now(_LONDON)
    today = _london_today(at)
    stored_day, count = _STATE.daily_opens.get(key, (today, 0))
    if stored_day != today:
        count = 0
    count += 1
    _STATE.daily_opens[key] = (today, count)
    return count


def daily_trade_count(epic: str, now: datetime | None = None) -> int:
    key = str(epic or "").strip()
    if not key:
        return 0
    at = now or datetime.now(_LONDON)
    today = _london_today(at)
    stored_day, count = _STATE.daily_opens.get(key, (today, 0))
    if stored_day != today:
        return 0
    return count


def check_daily_trade_cap(
    epic: str,
    cfg: Config,
    now: datetime | None = None,
    *,
    market: str | None = None,
) -> tuple[bool, str]:
    if not _enabled(cfg):
        return False, ""
    ep = _ep_cfg(cfg)
    cap = int(ep.get("max_trades_per_epic_per_day", 8))
    if cap <= 0:
        return False, ""
    used = daily_trade_count(epic, now=now)
    if used < cap:
        return False, ""
    label = str(market or epic)
    log_engine(
        f"[DAILY CAP] {label} entry suppressed — {used}/{cap} trades used today"
    )
    return True, f"{used}/{cap} trades used today"


def check_ranging_regime(
    signal_engine: Any,
    market: str,
    cfg: Config,
) -> tuple[bool, str]:
    if not _enabled(cfg):
        return False, ""
    ep = _ep_cfg(cfg)
    if not bool(ep.get("ranging_filter_enabled", True)):
        return False, ""
    threshold = float(ep.get("ranging_atr_ratio_threshold", 1.5))
    bars_needed = int(ep.get("ranging_h1_bars", 20))
    df = signal_engine.quote_df(market)
    c60 = signal_engine.candles(df, 60)
    if len(c60) < bars_needed:
        return False, ""
    c60i = signal_engine.add_indicators(c60)
    window = c60i.tail(bars_needed)
    if window["atr"].isna().all():
        return False, ""
    high_max = float(window["high"].max())
    low_min = float(window["low"].min())
    atr_mean = float(window["atr"].mean())
    if atr_mean <= 0:
        return False, ""
    ratio = (high_max - low_min) / atr_mean
    if ratio >= threshold:
        return False, ""
    log_engine(
        f"[REGIME BLOCK] {market} entry suppressed — ranging market detected "
        f"(ratio: {ratio:.1f})"
    )
    return True, f"ranging market detected (ratio: {ratio:.1f})"


def ml_training_record_count() -> int:
    try:
        from data.ml_training_store import MLTrainingStore

        return int(MLTrainingStore().record_count())
    except Exception:
        return 0


def ml_insufficient_data_threshold(cfg: Config) -> float | None:
    if not _enabled(cfg):
        return None
    ep = _ep_cfg(cfg)
    min_rows = int(ep.get("ml_min_rows_for_trust", 50))
    forced = float(ep.get("ml_insufficient_rows_threshold", 99))
    count = ml_training_record_count()
    if count < min_rows:
        return forced
    return None


def log_ml_insufficient_data_warning(cfg: Config | None = None) -> None:
    from system.config_loader import get_config

    active = cfg or get_config()
    if not _enabled(active):
        return
    ep = _ep_cfg(active)
    min_rows = int(ep.get("ml_min_rows_for_trust", 50))
    count = ml_training_record_count()
    if count < min_rows:
        log_engine(
            f"[ML WARNING] Store has {count} rows — insufficient for reliable classification"
        )
