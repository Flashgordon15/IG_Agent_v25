"""Sequential gate funnel telemetry — first-failure counts flushed to disk."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_FIRST_BLOCK_COUNTS: dict[str, dict[str, int]] = {}
_TOTAL_TICKS = 0
_ALL_PASSED_TICKS = 0
_LAST_FLUSH_MONO = 0.0
_FLUSH_INTERVAL_SEC = 30.0
_PROCESS_STARTED_AT = time.time()
_WINDOW_STARTED_AT = time.time()


def _report_path() -> Path:
    from system.paths import data_dir

    return Path(data_dir()) / "gate_funnel_report.json"


def _refuse_prod_mutate_under_test(path: Path) -> bool:
    if not (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    ):
        return False
    try:
        from system.paths import project_root

        resolved = path.resolve()
        prod = (project_root() / "src" / "data" / "v31-production").resolve()
        return str(resolved).startswith(str(prod) + os.sep) or resolved == prod
    except OSError:
        return False


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


def _payload_locked() -> dict[str, Any]:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_epoch": time.time(),
        "process_started_at": _PROCESS_STARTED_AT,
        "window_started_at": _WINDOW_STARTED_AT,
        "pid": os.getpid(),
        "total_ticks": _TOTAL_TICKS,
        "all_passed_ticks": _ALL_PASSED_TICKS,
        "first_block_counts": {
            gate: dict(sorted(details.items(), key=lambda kv: (-kv[1], kv[0])))
            for gate, details in sorted(_FIRST_BLOCK_COUNTS.items())
        },
    }


def _flush_locked(now_mono: float | None = None) -> None:
    global _LAST_FLUSH_MONO

    payload = _payload_locked()
    path = _report_path()
    if _refuse_prod_mutate_under_test(path):
        _LAST_FLUSH_MONO = now_mono if now_mono is not None else time.monotonic()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    _LAST_FLUSH_MONO = now_mono if now_mono is not None else time.monotonic()


def reset_gate_funnel_counter_for_tests() -> None:
    global _FIRST_BLOCK_COUNTS, _TOTAL_TICKS, _ALL_PASSED_TICKS, _LAST_FLUSH_MONO
    global _PROCESS_STARTED_AT, _WINDOW_STARTED_AT

    with _LOCK:
        _FIRST_BLOCK_COUNTS = {}
        _TOTAL_TICKS = 0
        _ALL_PASSED_TICKS = 0
        _LAST_FLUSH_MONO = 0.0
        _PROCESS_STARTED_AT = time.time()
        _WINDOW_STARTED_AT = time.time()
    path = _report_path()
    if _refuse_prod_mutate_under_test(path):
        return
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


def classify_funnel_status(
    payload: dict[str, Any] | None,
    *,
    max_age_sec: float = 900.0,
) -> str:
    """Return ok | stale | empty | unavailable for review/supervisor honesty."""
    if not isinstance(payload, dict) or not payload:
        return "unavailable"
    ticks = int(payload.get("total_ticks") or 0)
    if ticks <= 0 and not payload.get("first_block_counts"):
        return "empty"
    age = None
    epoch = payload.get("updated_at_epoch")
    if epoch is not None:
        try:
            age = time.time() - float(epoch)
        except (TypeError, ValueError):
            age = None
    if age is None:
        updated = str(payload.get("updated_at") or "")
        if updated:
            try:
                # Accept Z or offset ISO
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = time.time() - ts.timestamp()
            except ValueError:
                age = None
    if age is not None and age > max_age_sec:
        return "stale"
    return "ok"


def read_funnel_snapshot(*, flush_memory: bool = False) -> dict[str, Any]:
    """Return funnel counters.

    Default is **read-only against disk** so a separate consumer process cannot
    clobber a live agent report with its own zeroed in-memory counters.
    Pass ``flush_memory=True`` only from the trading process (or tests) when
    you intend to publish this process's counters.
    """
    try:
        if flush_memory:
            with _LOCK:
                if time.monotonic() - _LAST_FLUSH_MONO >= _FLUSH_INTERVAL_SEC:
                    _flush_locked()
                memory = _payload_locked()
        else:
            with _LOCK:
                memory = _payload_locked() if _TOTAL_TICKS > 0 else None

        path = _report_path()
        disk: dict[str, Any] | None = None
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    disk = raw
            except (OSError, json.JSONDecodeError):
                disk = None

        if disk is not None:
            out = dict(disk)
            if memory is not None:
                out["in_memory"] = memory
            out["status"] = classify_funnel_status(out)
            return out
        if memory is not None:
            out = dict(memory)
            out["status"] = classify_funnel_status(out)
            return out
        return {"status": "unavailable", "total_ticks": 0, "all_passed_ticks": 0}
    except Exception:
        return {"status": "unavailable"}
