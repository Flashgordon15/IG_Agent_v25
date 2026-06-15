"""
Session-end flatten verification — bounded rapid retries + slow monitor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from system.engine_log import log_engine

ClockFn = Callable[[], float]
NotifyFn = Callable[[str], None]

_DEFAULT_BACKOFF = [30, 60, 120, 240, 480]
_SLOW_MONITOR_INTERVAL_SEC = 600.0
_SLOW_MONITOR_ALERT_SEC = 3600.0


def _flatten_cfg(cfg: Any | None = None) -> dict[str, Any]:
    if cfg is None:
        from system.config_loader import get_config

        cfg = get_config()
    raw: dict[str, Any] = {}
    if hasattr(cfg, "get"):
        nested = cfg.get("flatten_retry")
        if isinstance(nested, dict):
            raw.update(nested)
    top = cfg._data if hasattr(cfg, "_data") else {}
    if isinstance(top, dict):
        nested = top.get("flatten_retry")
        if isinstance(nested, dict):
            raw = {**nested, **raw}
        for key in (
            "flatten_max_retries",
            "flatten_retry_backoff_seconds",
            "flatten_slow_monitor_interval_seconds",
        ):
            if key in top:
                raw[key] = top[key]
    return raw


def flatten_max_retries(cfg: Any | None = None) -> int:
    ep = _flatten_cfg(cfg)
    return int(ep.get("flatten_max_retries", 5))


def flatten_backoff_seconds(cfg: Any | None = None) -> list[int]:
    ep = _flatten_cfg(cfg)
    raw = ep.get("flatten_retry_backoff_seconds", _DEFAULT_BACKOFF)
    if isinstance(raw, (list, tuple)):
        out = [int(x) for x in raw]
        return out if out else list(_DEFAULT_BACKOFF)
    return list(_DEFAULT_BACKOFF)


def flatten_slow_monitor_interval(cfg: Any | None = None) -> float:
    ep = _flatten_cfg(cfg)
    return float(ep.get("flatten_slow_monitor_interval_seconds", _SLOW_MONITOR_INTERVAL_SEC))


@dataclass
class FlattenRetryState:
    epic: str = ""
    retry_count: int = 0
    abandoned: bool = False
    slow_monitor_active: bool = False
    slow_monitor_started_at: float | None = None
    last_attempt_at: float | None = None
    next_attempt_at: float | None = None
    telegram_urgent_sent: bool = False
    telegram_slow_sent: bool = False


_STATE = FlattenRetryState()


def get_flatten_retry_state() -> FlattenRetryState:
    return _STATE


def reset_flatten_retry_state() -> None:
    global _STATE
    _STATE = FlattenRetryState()


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


def on_flatten_verify_failed(
    epic: str,
    open_count: int,
    *,
    cfg: Any | None = None,
    now: float | None = None,
    notify: NotifyFn | None = None,
) -> FlattenRetryState:
    """Schedule next rapid retry or enter slow-monitor phase."""
    global _STATE
    at = float(now if now is not None else time.time())
    _STATE.epic = str(epic or "")
    max_retries = flatten_max_retries(cfg)
    backoff = flatten_backoff_seconds(cfg)

    if _STATE.abandoned and _STATE.slow_monitor_active:
        return _STATE

    if _STATE.retry_count < max_retries:
        idx = min(_STATE.retry_count, len(backoff) - 1)
        delay = float(backoff[idx])
        _STATE.retry_count += 1
        _STATE.last_attempt_at = at
        _STATE.next_attempt_at = at + delay
        log_engine(
            f"flatten verify failed — {open_count} position(s) still open "
            f"(retry {_STATE.retry_count}/{max_retries}, next in {int(delay)}s)"
        )
        if _STATE.retry_count >= max_retries:
            _STATE.abandoned = True
            _STATE.slow_monitor_active = True
            _STATE.slow_monitor_started_at = at
            _STATE.next_attempt_at = at + flatten_slow_monitor_interval(cfg)
            msg = (
                f"[FLATTEN ABANDONED] {epic} — {max_retries} retries exhausted, "
                "slow monitor active"
            )
            log_engine(msg)
            if not _STATE.telegram_urgent_sent:
                _STATE.telegram_urgent_sent = True
                _notify_urgent(f"🚨 URGENT: {msg}", notify=notify)
        return _STATE

    _STATE.abandoned = True
    _STATE.slow_monitor_active = True
    if _STATE.slow_monitor_started_at is None:
        _STATE.slow_monitor_started_at = at
    return _STATE


def on_flatten_confirmed() -> None:
    """Clear retry state when IG confirms flat."""
    global _STATE
    if _STATE.retry_count or _STATE.slow_monitor_active:
        log_engine("[FLATTEN CONFIRMED] all positions closed — monitor cleared")
    reset_flatten_retry_state()


def should_run_flatten_retry(
    *,
    cfg: Any | None = None,
    now: float | None = None,
) -> bool:
    """True when a scheduled rapid retry or slow-monitor poll is due."""
    at = float(now if now is not None else time.time())
    if _STATE.next_attempt_at is None:
        return False
    return at >= float(_STATE.next_attempt_at)


def mark_flatten_retry_attempt(*, now: float | None = None) -> None:
    at = float(now if now is not None else time.time())
    _STATE.last_attempt_at = at
    if _STATE.slow_monitor_active:
        _STATE.next_attempt_at = at + flatten_slow_monitor_interval()
    else:
        _STATE.next_attempt_at = None


def check_slow_monitor_alerts(
    epic: str,
    open_count: int,
    *,
    now: float | None = None,
    notify: NotifyFn | None = None,
) -> None:
    """Second Telegram if still open 60 minutes after slow monitor started."""
    if not _STATE.slow_monitor_active or open_count <= 0:
        return
    if _STATE.telegram_slow_sent or _STATE.slow_monitor_started_at is None:
        return
    at = float(now if now is not None else time.time())
    elapsed = at - float(_STATE.slow_monitor_started_at)
    if elapsed >= _SLOW_MONITOR_ALERT_SEC:
        _STATE.telegram_slow_sent = True
        _notify_urgent(
            f"🚨 FLATTEN STILL OPEN — {epic}: {open_count} position(s) after 60m slow monitor",
            notify=notify,
        )


def flatten_retry_snapshot() -> dict[str, Any]:
    return {
        "epic": _STATE.epic,
        "retry_count": _STATE.retry_count,
        "abandoned": _STATE.abandoned,
        "slow_monitor_active": _STATE.slow_monitor_active,
        "next_attempt_at": _STATE.next_attempt_at,
    }
