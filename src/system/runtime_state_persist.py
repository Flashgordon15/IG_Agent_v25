"""
Crash-safe persistence for Japan225 runtime protections.

Persists the in-memory registries (entry-inflight, exit-inflight,
pending-order-reconcile, daily-risk counters) to a small JSON file so
critical runtime guards survive restart. Designed to be best-effort:

- atomic writes (temp file + fsync + os.replace)
- on load, missing or malformed payloads are ignored, with a single
  warning emitted so startup is never blocked
- mutations call request_save() which throttles to avoid excessive
  disk I/O during bursts; flush_save() bypasses the throttle

The strategy / risk logic / session scheduler are not touched — this
module only mirrors registry state to disk and back.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

STATE_VERSION = 1
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime_state.json"
_path_override: Path | None = None
_lock = threading.RLock()
_last_save_ts: float = 0.0
_min_write_interval_sec: float = 0.05
_corrupt_warned: bool = False


def _path() -> Path:
    return _path_override or _DEFAULT_PATH


def set_state_path_for_tests(path: Path | str | None) -> None:
    """Override the persistence path (tests only)."""
    global _path_override, _last_save_ts, _corrupt_warned
    with _lock:
        _path_override = Path(path) if path else None
        _last_save_ts = 0.0
        _corrupt_warned = False


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".rs_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _collect_state() -> dict[str, Any]:
    from execution.entry_inflight import dump_entry_state
    from execution.exit_inflight import dump_exit_state
    from execution.japan225_daily_risk import dump_daily_risk_state
    from execution.pending_order_reconcile import dump_pending_state

    return {
        "version": STATE_VERSION,
        "saved_at": time.time(),
        "entry": dump_entry_state(),
        "exit": dump_exit_state(),
        "pending": dump_pending_state(),
        "daily_risk": dump_daily_risk_state(),
    }


def request_save() -> None:
    """Persist current state. Throttled to avoid excessive writes during bursts."""
    global _last_save_ts
    now = time.time()
    with _lock:
        if now - _last_save_ts < _min_write_interval_sec:
            return
        _last_save_ts = now
    try:
        data = _collect_state()
        _atomic_write_json(_path(), data)
    except Exception as e:
        log_engine(f"runtime_state_persist save failed: {type(e).__name__}: {e}")


def flush_save() -> None:
    """Ignore throttle and write now."""
    global _last_save_ts
    try:
        data = _collect_state()
        _atomic_write_json(_path(), data)
    except Exception as e:
        log_engine(f"runtime_state_persist flush failed: {type(e).__name__}: {e}")
        return
    with _lock:
        _last_save_ts = time.time()


def load_state() -> bool:
    """Load persisted state into the in-memory registries.

    Returns True on success. Missing file → False (silent). Corrupt
    file → False with a single warning. Any partial-deserialise error
    leaves whichever registries did load in place.
    """
    global _corrupt_warned
    p = _path()
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("root not a dict")
    except Exception as e:
        with _lock:
            warned = _corrupt_warned
            _corrupt_warned = True
        if not warned:
            log_engine(
                f"runtime_state_persist load failed (corrupt — ignored): "
                f"{type(e).__name__}: {e}"
            )
        return False

    try:
        from execution.entry_inflight import load_entry_state
        from execution.exit_inflight import load_exit_state
        from execution.japan225_daily_risk import load_daily_risk_state
        from execution.pending_order_reconcile import load_pending_state

        load_entry_state(data.get("entry") or {})
        load_exit_state(data.get("exit") or {})
        load_pending_state(data.get("pending") or {})
        load_daily_risk_state(data.get("daily_risk") or {})
        return True
    except Exception as e:
        log_engine(
            f"runtime_state_persist load partial failure: {type(e).__name__}: {e}"
        )
        return False


def reconcile_with_broker(rest_client: Any, epic: str) -> None:
    """After loading persisted state, clear entries that disagree with broker.

    - Entry pending/inflight + no broker position → cleared (order never
      filled across restart).
    - Exit pending/inflight + broker still shows position → cleared
      (close never executed across restart).
    """
    if rest_client is None or not epic:
        return
    try:
        if hasattr(rest_client, "count_open_positions"):
            count = int(rest_client.count_open_positions(epic))
        elif hasattr(rest_client, "open_positions"):
            count = sum(
                1
                for item in rest_client.open_positions()
                if (item.get("market") or {}).get("epic") == epic
            )
        else:
            return
    except Exception as e:
        log_engine(
            f"runtime_state_persist reconcile broker query failed: "
            f"{type(e).__name__}: {e}"
        )
        return

    from execution.entry_inflight import clear_entry, has_entry_in_flight
    from execution.exit_inflight import clear_exit, has_exit_in_flight
    from execution.pending_order_reconcile import (
        ORDER_TYPE_ENTRY,
        ORDER_TYPE_EXIT,
        get_pending,
        resolve_pending,
    )

    position_present = count > 0

    if has_entry_in_flight(epic) and not position_present:
        clear_entry(epic)
    if has_exit_in_flight(epic) and position_present:
        clear_exit(epic)

    pending = get_pending(epic)
    if pending is not None:
        if pending.order_type == ORDER_TYPE_ENTRY and position_present:
            resolve_pending(epic, reason="entry confirmed by startup reconciliation")
        elif pending.order_type == ORDER_TYPE_ENTRY and not position_present:
            resolve_pending(epic, reason="no broker position — startup cleared pending entry")
        elif pending.order_type == ORDER_TYPE_EXIT and not position_present:
            resolve_pending(epic, reason="exit confirmed by startup reconciliation")

    flush_save()


def reset_persist_state_for_tests() -> None:
    global _last_save_ts, _corrupt_warned, _path_override
    with _lock:
        _last_save_ts = 0.0
        _corrupt_warned = False
        _path_override = None
