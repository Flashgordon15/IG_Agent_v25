"""
IG REST traffic governor — hard cap on POST /v1/positions/otc transaction payloads.

Drops excess order signals when more than 3 transaction payloads would be sent in a
rolling 60-second window, protecting the broker account from rate exhaustion.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, project_root

MAX_TX_PER_60S = 3
WINDOW_SEC = 60.0

AUDIT_LOG = project_root() / "src" / "data" / "logs" / "self_healing_audit.log"
AUDIT_LOG_PRODUCTION = data_dir() / "logs" / "self_healing_audit.log"

_lock = threading.Lock()
_transmit_times: deque[float] = deque()
_audit_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_audit(record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str)
    for path in (AUDIT_LOG, AUDIT_LOG_PRODUCTION):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _audit_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            log_guarded_exception("ig_rest_traffic_governor_audit", exc)


def _prune(now: float) -> None:
    cutoff = now - WINDOW_SEC
    while _transmit_times and _transmit_times[0] < cutoff:
        _transmit_times.popleft()


def consume_positions_otc_transmit_slot(
    *,
    epic: str = "",
    label: str = "POST /v1/positions/otc",
) -> tuple[bool, str]:
    """
    Acquire a transmit slot for POST /v1/positions/otc.

    Returns (allowed, reason). When not allowed the caller must drop the order.
    """
    now = time.time()
    with _lock:
        _prune(now)
        if len(_transmit_times) >= MAX_TX_PER_60S:
            reason = (
                f"IG REST traffic governor: {len(_transmit_times)} transaction payloads "
                f"in rolling {int(WINDOW_SEC)}s — dropped excess signal"
            )
            log_engine(f"[IGRestGovernor] {reason} epic={epic} label={label}")
            _append_audit(
                {
                    "ts": _utc_iso(),
                    "component": "ig_rest_traffic_governor",
                    "event": "order_dropped",
                    "epic": epic,
                    "label": label,
                    "window_sec": WINDOW_SEC,
                    "max_tx": MAX_TX_PER_60S,
                    "message": reason,
                }
            )
            return False, reason
        _transmit_times.append(now)
        return True, ""


def reset_ig_rest_traffic_governor_for_tests() -> None:
    with _lock:
        _transmit_times.clear()
