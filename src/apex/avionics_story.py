"""Timestamped avionics lifecycle narrative — IPC fan-out for dashboard storytelling."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

_STORY_LOCK = threading.RLock()
_STORY: deque[dict[str, Any]] = deque(maxlen=128)
_HEARTBEAT_STARTED = False
_HEARTBEAT_LOCK = threading.Lock()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def append_avionics_story(
    message: str,
    *,
    kind: str = "info",
    epic: str = "",
) -> dict[str, Any]:
    """Record a human-readable lifecycle line and broadcast over apex_ipc.sock."""
    line = str(message or "").strip()
    if not line:
        return {}
    entry = {
        "ts": _utc_stamp(),
        "line": line,
        "kind": str(kind or "info"),
        "epic": str(epic or ""),
    }
    with _STORY_LOCK:
        _STORY.appendleft(entry)
    try:
        from apex.ipc_bridge import broadcast_story_event

        broadcast_story_event(entry)
    except Exception:
        pass
    return entry


def snapshot_avionics_stories(*, limit: int = 48) -> list[dict[str, Any]]:
    with _STORY_LOCK:
        return [dict(row) for row in list(_STORY)[: max(1, int(limit))]]


def reset_avionics_story_for_tests() -> None:
    with _STORY_LOCK:
        _STORY.clear()


def _weekend_or_frozen_ticks() -> bool:
    try:
        from system.agent_execution_mode import force_market_open_active

        if force_market_open_active():
            return False
    except Exception:
        pass
    try:
        from system.market_watch.calendar import is_market_open
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry

        cfg = get_config()
        for _iid, inst in InstrumentRegistry(cfg.as_dict()).get_enabled_with_ids():
            epic = str(inst.get("epic") or "").strip()
            if epic and is_market_open(epic):
                return False
        return True
    except Exception:
        return True


def start_weekend_heartbeat_daemon() -> None:
    """Scroll internal heartbeat logs every 3s when live ticks are frozen (weekend)."""
    global _HEARTBEAT_STARTED
    with _HEARTBEAT_LOCK:
        if _HEARTBEAT_STARTED:
            return
        _HEARTBEAT_STARTED = True

    def _loop() -> None:
        while True:
            try:
                if _weekend_or_frozen_ticks():
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    append_avionics_story(
                        f"Internal heartbeat — engine alive · {stamp}",
                        kind="heartbeat",
                    )
                    try:
                        from apex.system_monitor import append_monitor_line

                        append_monitor_line(
                            "HEARTBEAT",
                            "Weekend tick backfill — microkernel alive",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(3.0)

    threading.Thread(
        target=_loop, name="avionics-weekend-heartbeat", daemon=True
    ).start()


def reset_weekend_heartbeat_for_tests() -> None:
    global _HEARTBEAT_STARTED
    with _HEARTBEAT_LOCK:
        _HEARTBEAT_STARTED = False
