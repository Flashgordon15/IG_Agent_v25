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


def _effective_max_tx_per_window() -> int:
    """Canary/live-canary may raise the hard cap; 0 = unlimited."""
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active():
            block = {}
            try:
                from system.config_loader import get_config

                block = get_config().get("demo_throughput_mode") or {}
            except Exception:
                pass
            if bool(block.get("bypass_traffic_governor", True)):
                try:
                    demo_max = int(block.get("demo_max_tx_per_60s") or 6)
                except (TypeError, ValueError):
                    demo_max = 6
                return demo_max if demo_max > 0 else 0
    except Exception:
        pass
    try:
        from system.config_loader import get_config

        cfg = get_config()
        lc = cfg.get("live_canary") if hasattr(cfg, "get") else {}
        if isinstance(lc, dict) and lc.get("enabled"):
            if lc.get("bypass_traffic_governor"):
                return 0
            override = int(lc.get("ig_rest_max_tx_per_60s") or 0)
            if override > 0:
                return override
    except Exception:
        pass
    return MAX_TX_PER_60S

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


def positions_otc_transmit_slot_available() -> bool:
    """Peek — True when a POST /positions/otc would be allowed (does not consume)."""
    max_tx = _effective_max_tx_per_window()
    if max_tx <= 0:
        return True
    now = time.time()
    with _lock:
        _prune(now)
        return len(_transmit_times) < max_tx


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
    max_tx = _effective_max_tx_per_window()
    with _lock:
        _prune(now)
        if max_tx > 0 and len(_transmit_times) >= max_tx:
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
