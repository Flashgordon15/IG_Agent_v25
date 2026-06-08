"""Persist feeder tail offsets for v26 shadow catch-up on agent restart."""

from __future__ import annotations

import json
import threading
from typing import Any

from system.paths import project_root

_lock = threading.RLock()
_PATH = project_root() / "data_lake" / "state" / "shadow_tail_offsets.json"


def _read() -> dict[str, Any]:
    if not _PATH.is_file():
        return {"offsets": {}}
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"offsets": {}}
    except (json.JSONDecodeError, OSError):
        return {"offsets": {}}


def load_offset(day: str) -> int | None:
    with _lock:
        offsets = _read().get("offsets") or {}
        if day in offsets:
            try:
                return int(offsets[day])
            except (TypeError, ValueError):
                return None
    return None


def save_offset(day: str, offset: int) -> None:
    with _lock:
        data = _read()
        offsets = dict(data.get("offsets") or {})
        offsets[day] = max(0, int(offset))
        data["offsets"] = offsets
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reset_shadow_offsets_for_tests() -> None:
    with _lock:
        try:
            _PATH.unlink(missing_ok=True)
        except OSError:
            pass
