"""Virtual wall clock driven by historical replay timestamps (HARDENED_TESTBED)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

_replay_epoch: Optional[float] = None
_replay_active: bool = False
_lock = threading.Lock()


def set_replay_time(ts: float | datetime) -> None:
    """Advance the virtual clock to *ts* (epoch seconds or aware/naive UTC datetime)."""
    global _replay_epoch, _replay_active
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        epoch = ts.timestamp()
    else:
        epoch = float(ts)
    with _lock:
        _replay_epoch = epoch
        _replay_active = True


def clear_replay_clock() -> None:
    global _replay_epoch, _replay_active
    with _lock:
        _replay_epoch = None
        _replay_active = False


def is_replay_active() -> bool:
    with _lock:
        return _replay_active and _replay_epoch is not None


def now() -> float:
    """Current time as epoch seconds — replay virtual time when active, else wall clock."""
    with _lock:
        if _replay_epoch is not None:
            return _replay_epoch
    return time.time()


def now_datetime() -> datetime:
    return datetime.fromtimestamp(now(), tz=timezone.utc)


def monotonic() -> float:
    """Monotonic surrogate aligned to replay epoch (stable ordering within a replay)."""
    return now()
