"""Sequential gate funnel telemetry — first-failure counts flushed to disk."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from system.paths import data_dir

_LOCK = threading.Lock()
_FIRST_BLOCK_COUNTS: dict[str, dict[str, int]] = {}
_TOTAL_TICKS = 0
_ALL_PASSED_TICKS = 0
_LAST_FLUSH_MONO = 0.0
_FLUSH_INTERVAL_SEC = 30.0


def _report_path():
    return data_dir() / "gate_funnel_report.json"


def record_sequential_gate_funnel(gates: list[Any]) -> None:
    """
    Walk ``gates`` in order; count the first ``passed=False`` gate by name + detail.

    Non-blocking — failures are swallowed. Does not affect gate pass/fail logic.
    """
    global _TOTAL_TICKS, _ALL_PASSED_TICKS, _LAST_FLUSH_MONO

    try:
        with _LOCK:
            _TOTAL_TICKS += 1
            first_fail = next((g for g in gates if not g.passed), None)
            if first_fail is None:
                _ALL_PASSED_TICKS += 1
            else:
                gate_name = str(getattr(first_fail, "name", "") or "unknown")
                detail = str(getattr(first_fail, "detail", "") or "").strip() or "(no detail)"
                by_detail = _FIRST_BLOCK_COUNTS.setdefault(gate_name, {})
                by_detail[detail] = by_detail.get(detail, 0) + 1

            now = time.monotonic()
            if now - _LAST_FLUSH_MONO >= _FLUSH_INTERVAL_SEC:
                _flush_locked(now)
    except Exception:
        pass


def _flush_locked(now_mono: float | None = None) -> None:
    global _LAST_FLUSH_MONO

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_ticks": _TOTAL_TICKS,
        "all_passed_ticks": _ALL_PASSED_TICKS,
        "first_block_counts": {
            gate: dict(sorted(details.items(), key=lambda kv: (-kv[1], kv[0])))
            for gate, details in sorted(_FIRST_BLOCK_COUNTS.items())
        },
    }
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    _LAST_FLUSH_MONO = now_mono if now_mono is not None else time.monotonic()


def reset_gate_funnel_counter_for_tests() -> None:
    global _FIRST_BLOCK_COUNTS, _TOTAL_TICKS, _ALL_PASSED_TICKS, _LAST_FLUSH_MONO

    with _LOCK:
        _FIRST_BLOCK_COUNTS = {}
        _TOTAL_TICKS = 0
        _ALL_PASSED_TICKS = 0
        _LAST_FLUSH_MONO = 0.0
    path = _report_path()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def flush_gate_funnel_report() -> None:
    """Force-write the in-memory funnel report (tests / shutdown hooks)."""
    try:
        with _LOCK:
            _flush_locked()
    except Exception:
        pass


def read_funnel_snapshot() -> dict[str, Any]:
    """Return in-memory funnel counters, flushing to disk when stale."""
    try:
        with _LOCK:
            if time.monotonic() - _LAST_FLUSH_MONO >= _FLUSH_INTERVAL_SEC:
                _flush_locked()
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "total_ticks": _TOTAL_TICKS,
                "all_passed_ticks": _ALL_PASSED_TICKS,
                "first_block_counts": {
                    gate: dict(details)
                    for gate, details in _FIRST_BLOCK_COUNTS.items()
                },
            }
        path = _report_path()
        if path.is_file():
            try:
                disk = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(disk, dict):
                    disk.setdefault("in_memory", payload)
                    return disk
            except (OSError, json.JSONDecodeError):
                pass
        return payload
    except Exception:
        return {}
