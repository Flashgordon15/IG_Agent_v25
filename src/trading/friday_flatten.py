"""
Friday hard flatten — close all positions before weekend (Europe/London).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from system.config import Config
from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_MONITOR_START = time(19, 0)
_DEFAULT_FLATTEN = time(19, 30)
_DEFAULT_CONFIRM = time(19, 45)
_CHECK_INTERVAL_SEC = 60.0

CloseFn = Callable[[], int]
VerifyFn = Callable[[datetime], None]
OpenCountFn = Callable[[], int]
PositionsFn = Callable[[], list[dict[str, Any]]]
NotifyFn = Callable[[str], None]


@dataclass
class FridayFlattenState:
    active: bool = False
    phase: str = "idle"  # idle | monitoring | initiated | confirmed | alerted
    week_key: str = ""
    last_check_mono: float = 0.0
    flatten_initiated: bool = False
    confirm_checked: bool = False
    positions_at_init: int = 0


_STATE = FridayFlattenState()
_LOCK = threading.Lock()


def get_friday_flatten_state() -> FridayFlattenState:
    return _STATE


def reset_friday_flatten_state() -> None:
    global _STATE
    with _LOCK:
        _STATE = FridayFlattenState()


def _ff_cfg(cfg: Config) -> dict[str, Any]:
    raw = cfg.get("friday_flatten")
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_time(value: str, default: time) -> time:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return default
    hour, minute = (int(x) for x in text.split(":", 1))
    return time(hour, minute)


def friday_flatten_enabled(cfg: Config) -> bool:
    return bool(_ff_cfg(cfg).get("enabled", True))


def friday_flatten_override(cfg: Config) -> bool:
    ep = _ff_cfg(cfg)
    if bool(ep.get("friday_flatten_override", False)):
        return True
    try:
        from system.config_loader import get_config

        store = getattr(get_config(), "_store", None)
    except Exception:
        store = None
    if store is not None and hasattr(store, "get_runtime_state"):
        try:
            return str(store.get_runtime_state("friday_flatten_override") or "") == "1"
        except Exception:
            pass
    return False


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _london_now(now: datetime | None = None) -> datetime:
    at = now or datetime.now(_LONDON)
    if at.tzinfo is None:
        return at.replace(tzinfo=_LONDON)
    return at.astimezone(_LONDON)


def _is_friday(at: datetime) -> bool:
    return at.weekday() == 4


def _time_reached(at: datetime, target: time) -> bool:
    t = at.time()
    return t >= target


def _position_opened_before_flatten(
    row: dict[str, Any], flatten_at: datetime
) -> bool:
    """Skip positions opened at/after flatten trigger (session blackout guard)."""
    opened = row.get("opened_at") or row.get("entry_time")
    if not opened:
        return True
    try:
        text = str(opened).replace("Z", "+00:00")
        opened_dt = datetime.fromisoformat(text)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=_LONDON)
        else:
            opened_dt = opened_dt.astimezone(_LONDON)
        return opened_dt < flatten_at
    except Exception:
        return True


def friday_flatten_snapshot(cfg: Config | None = None) -> dict[str, Any]:
    """Dashboard/runtime awareness payload."""
    from system.config_loader import get_config

    active_cfg = cfg or get_config()
    at = _london_now()
    ep = _ff_cfg(active_cfg)
    flatten_t = _parse_time(str(ep.get("friday_flatten_time_bst", "19:30")), _DEFAULT_FLATTEN)
    confirm_t = _parse_time(
        str(ep.get("friday_flatten_confirm_time_bst", "19:45")), _DEFAULT_CONFIRM
    )
    with _LOCK:
        st = _STATE
        return {
            "enabled": friday_flatten_enabled(active_cfg),
            "override": friday_flatten_override(active_cfg),
            "active": st.active,
            "phase": st.phase,
            "is_friday": _is_friday(at),
            "flatten_time_bst": flatten_t.strftime("%H:%M"),
            "confirm_time_bst": confirm_t.strftime("%H:%M"),
            "banner": st.active and st.phase in ("monitoring", "initiated"),
        }


def _notify_urgent(message: str, notify: NotifyFn | None = None) -> None:
    log_engine(message)
    if notify is not None:
        notify(message)
        return
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send(message)
    except Exception:
        pass


def run_friday_flatten_tick(
    *,
    cfg: Config,
    now: datetime | None = None,
    execute_close: CloseFn,
    verify_close: VerifyFn,
    open_count_fn: OpenCountFn,
    list_positions_fn: PositionsFn | None = None,
    notify: NotifyFn | None = None,
    mono_now: float | None = None,
) -> None:
    """Called from trading_loop each tick — idempotent across epics."""
    if not friday_flatten_enabled(cfg):
        with _LOCK:
            _STATE.active = False
            _STATE.phase = "idle"
        return
    if friday_flatten_override(cfg):
        with _LOCK:
            _STATE.active = False
            _STATE.phase = "idle"
        return

    at = _london_now(now)
    if not _is_friday(at):
        with _LOCK:
            if _STATE.week_key and _STATE.week_key != _week_key(at):
                reset_friday_flatten_state()
            _STATE.active = False
            _STATE.phase = "idle"
        return

    ep = _ff_cfg(cfg)
    flatten_t = _parse_time(str(ep.get("friday_flatten_time_bst", "19:30")), _DEFAULT_FLATTEN)
    confirm_t = _parse_time(
        str(ep.get("friday_flatten_confirm_time_bst", "19:45")), _DEFAULT_CONFIRM
    )
    wk = _week_key(at)

    import time as _time

    mono = float(mono_now if mono_now is not None else _time.monotonic())

    with _LOCK:
        if _STATE.week_key != wk:
            _STATE.week_key = wk
            _STATE.flatten_initiated = False
            _STATE.confirm_checked = False
            _STATE.phase = "idle"
        if at.time() < _MONITOR_START:
            _STATE.active = False
            return
        _STATE.active = True
        if _STATE.phase == "idle":
            _STATE.phase = "monitoring"
        if mono - _STATE.last_check_mono < _CHECK_INTERVAL_SEC and not _time_reached(
            at, flatten_t
        ) and not _time_reached(at, confirm_t):
            return
        _STATE.last_check_mono = mono

    open_count = int(open_count_fn())
    if open_count <= 0:
        with _LOCK:
            if _time_reached(at, confirm_t) and not _STATE.confirm_checked:
                _STATE.confirm_checked = True
                _STATE.phase = "confirmed"
                log_engine(
                    "[FRIDAY FLATTEN] Confirmed flat at "
                    f"{confirm_t.strftime('%H:%M')} BST — safe for weekend"
                )
        return

    if _time_reached(at, flatten_t) and not _STATE.flatten_initiated:
        with _LOCK:
            if _STATE.flatten_initiated:
                return
            _STATE.flatten_initiated = True
            _STATE.phase = "initiated"
            _STATE.positions_at_init = open_count
        log_engine(
            f"[FRIDAY FLATTEN] Initiating weekend close — {open_count} position(s) open"
        )
        if list_positions_fn is not None:
            for row in list_positions_fn():
                if not _position_opened_before_flatten(row, at):
                    continue
                epic = str(row.get("epic") or "")
                entry = row.get("entry")
                pnl = row.get("unrealized_pnl") or row.get("pnl_gbp")
                log_engine(
                    f"[FRIDAY FLATTEN] Position closed: {epic} entry {entry} "
                    f"current — P&L {pnl}"
                )
        try:
            closed = int(execute_close())
            log_engine(f"[FRIDAY FLATTEN] Close commands sent — {closed} position(s)")
        except Exception as e:
            log_engine(f"[FRIDAY FLATTEN] Close failed: {type(e).__name__}: {e}")
        verify_close(at)
        return

    if _time_reached(at, confirm_t):
        with _LOCK:
            if _STATE.confirm_checked:
                return
            _STATE.confirm_checked = True
        remaining = int(open_count_fn())
        if remaining <= 0:
            with _LOCK:
                _STATE.phase = "confirmed"
            log_engine(
                "[FRIDAY FLATTEN] Confirmed flat at "
                f"{confirm_t.strftime('%H:%M')} BST — safe for weekend"
            )
            return
        with _LOCK:
            _STATE.phase = "alerted"
        details: list[str] = []
        if list_positions_fn is not None:
            for row in list_positions_fn():
                details.append(
                    f"{row.get('epic')} {row.get('side')} deal={row.get('ig_deal_id')}"
                )
        detail_txt = "; ".join(details) if details else f"{remaining} open"
        msg = (
            "[FRIDAY FLATTEN ALERT] Position failed to close — "
            f"manual intervention required ({detail_txt})"
        )
        _notify_urgent(f"🚨 URGENT: {msg}", notify=notify)
