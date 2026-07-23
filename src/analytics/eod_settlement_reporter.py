"""End-of-day settlement reporter — async PERF worker, disk-only I/O.

Writes daily settlement summaries under ``data_dir()/metrics/`` (v31-production
when IG_DATA_ROOT / desk mode applies). Never scheduled on the tick lane.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from system.engine_log import log_engine

_LONDON = ZoneInfo("Europe/London")
_POLL_SEC = float(os.environ.get("IG_EOD_REPORTER_POLL_SEC", "60"))
_TARGET_HHMM = str(os.environ.get("IG_EOD_REPORTER_HHMM", "21:55")).strip() or "21:55"

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_started = False
_last_report_day: str = ""
_last_result: dict[str, Any] = {"ok": False, "ts": 0.0}


def reset_eod_reporter_for_tests() -> None:
    global _started, _thread, _last_report_day, _last_result
    stop_eod_settlement_reporter()
    with _lock:
        _started = False
        _thread = None
        _last_report_day = ""
        _last_result = {"ok": False, "ts": 0.0}


def metrics_dir() -> Path:
    """Resolve metrics root via ``data_dir()`` (bridges to v31-production)."""
    from system.paths import data_dir

    d = data_dir() / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settlement_log_path(*, day: str | None = None) -> Path:
    stamp = day or datetime.now(_LONDON).strftime("%Y-%m-%d")
    return metrics_dir() / f"eod_settlement_{stamp}.json"


def _london_now() -> datetime:
    return datetime.now(_LONDON)


def _parse_target_hhmm(raw: str) -> tuple[int, int]:
    parts = str(raw or "21:55").split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, hh)), max(0, min(59, mm))
    except (TypeError, ValueError):
        return 21, 55


def _realized_today_gbp() -> float:
    try:
        from diagnostics.performance_journal import milestone_progress_payload

        mile = milestone_progress_payload()
        return float(mile.get("daily_realized_pnl_gbp") or 0.0)
    except Exception:
        pass
    # Fallback: sum journal CSV for London calendar day
    try:
        from diagnostics.performance_journal import journal_path

        path = journal_path()
        if not path.is_file():
            return 0.0
        today = _london_now().strftime("%Y-%m-%d")
        total = 0.0
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts = str(row.get("Timestamp") or "")
                if not ts.startswith(today):
                    continue
                try:
                    total += float(row.get("RealizedPnL_GBP") or 0.0)
                except (TypeError, ValueError):
                    continue
        return total
    except Exception:
        return 0.0


def _broker_open_count() -> int:
    try:
        from system.unified_fulfillment_cache import get_fulfillment_snapshot

        snap = get_fulfillment_snapshot() or {}
        opens = snap.get("open_positions") or snap.get("positions") or []
        if isinstance(opens, list):
            return len(opens)
    except Exception:
        pass
    return 0


def build_settlement_report(*, force_day: str | None = None) -> dict[str, Any]:
    """Pure-ish report builder (disk reads only)."""
    now = _london_now()
    day = force_day or now.strftime("%Y-%m-%d")
    realized = _realized_today_gbp()
    report = {
        "ok": True,
        "report_type": "eod_settlement",
        "day_london": day,
        "generated_at_london": now.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "daily_realized_pnl_gbp": round(realized, 4),
        "broker_open_count": _broker_open_count(),
        "flat": _broker_open_count() == 0,
        "source": "eod_settlement_reporter",
    }
    return report


def write_settlement_report(
    report: dict[str, Any] | None = None,
    *,
    force_day: str | None = None,
) -> dict[str, Any]:
    """Write JSON settlement log under metrics/. Closes file handle on exit."""
    payload = report if report is not None else build_settlement_report(force_day=force_day)
    day = str(payload.get("day_london") or force_day or _london_now().strftime("%Y-%m-%d"))
    path = settlement_log_path(day=day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        # Also append a one-line JSONL ledger for chron order
        ledger = metrics_dir() / "eod_settlement.jsonl"
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
            fh.flush()
        result = {
            "ok": True,
            "path": str(path),
            "day_london": day,
            "ts": time.time(),
            "daily_realized_pnl_gbp": payload.get("daily_realized_pnl_gbp"),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "ts": time.time(),
            "day_london": day,
        }
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass

    with _lock:
        global _last_result, _last_report_day
        _last_result = dict(result)
        if result.get("ok"):
            _last_report_day = day
    return result


def maybe_run_eod_settlement(*, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """Fire once per London calendar day after target HH:MM (default 21:55)."""
    global _last_report_day
    ts = now or _london_now()
    day = ts.strftime("%Y-%m-%d")
    hh, mm = _parse_target_hhmm(_TARGET_HHMM)
    due = force or (ts.hour > hh or (ts.hour == hh and ts.minute >= mm))
    with _lock:
        already = _last_report_day == day
    if already and not force:
        return {"ok": True, "skipped": True, "reason": "already_reported", "day_london": day}
    if not due:
        return {"ok": True, "skipped": True, "reason": "before_window", "day_london": day}
    return write_settlement_report(force_day=day)


def last_eod_result() -> dict[str, Any]:
    with _lock:
        return dict(_last_result)


def _eod_loop() -> None:
    while not _stop.is_set():
        try:
            maybe_run_eod_settlement()
        except Exception as exc:
            log_engine(f"eod_settlement_reporter: loop error {type(exc).__name__}: {exc}")
        _stop.wait(max(15.0, float(_POLL_SEC)))


def start_eod_settlement_reporter() -> None:
    """Start PERF background EOD reporter. Idempotent."""
    global _thread, _started
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_eod_loop,
            name="perf-eod-settlement",
            daemon=True,
        )
        _thread.start()
        _started = True
    log_engine(
        f"eod_settlement_reporter: started (poll {_POLL_SEC:.0f}s, target {_TARGET_HHMM} London)"
    )


def stop_eod_settlement_reporter() -> None:
    global _started
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    with _lock:
        _started = False
