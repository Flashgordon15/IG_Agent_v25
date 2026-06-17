"""Permanent 24-hour supervisor triage ledger — JSONL append-only."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from system.paths import data_dir

_lock = threading.Lock()
_HISTORY_PATH = data_dir() / "logs" / "supervisor_history.jsonl"
_RETENTION_HOURS = 24.0


def history_path() -> Path:
    return _HISTORY_PATH


def record_supervisor_event(
    event_type: str,
    *,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    source: str = "self_healing_supervisor",
) -> None:
    """Non-blocking append — never raises to callers."""
    record = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_type": str(event_type),
        "source": source,
        "detail": detail,
        "payload": payload or {},
    }
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def read_history_last_24h(*, max_lines: int = 200) -> list[dict[str, Any]]:
    path = history_path()
    if not path.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_RETENTION_HOURS)
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                iso = row.get("iso") or ""
                try:
                    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
                if ts is not None and ts < cutoff:
                    continue
                out.append(row)
        if len(out) > max_lines:
            out = out[-max_lines:]
    except OSError:
        return []
    return out


def reset_supervisor_history_for_tests() -> None:
    try:
        history_path().unlink(missing_ok=True)
    except OSError:
        pass
