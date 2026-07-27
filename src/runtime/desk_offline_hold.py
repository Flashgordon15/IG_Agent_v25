"""Durable operator hold — Trading Desk / twins must not auto-boot while set.

Unlike ``manual_stop.json`` (default 10-minute window for brief Stop Agent),
this marker has **no age expiry**. Clear only via ``clear_desk_offline_hold``
or an explicit operator reopen.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_HOLD_NAME = "desk_offline_hold.json"


def _hold_paths() -> tuple[Path, ...]:
    from system.paths import data_dir, legacy_src_data_dir

    candidates = (
        data_dir() / "state" / _HOLD_NAME,
        legacy_src_data_dir() / "state" / _HOLD_NAME,
    )
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.parent.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return tuple(out)


def set_desk_offline_hold(*, reason: str = "operator_interim_offline") -> dict[str, Any]:
    payload = {
        "active": True,
        "reason": str(reason or "operator_interim_offline"),
        "ts": time.time(),
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Blocks auto-boot of Trading Desk UI / twins / watchdogs until cleared.",
    }
    written: list[str] = []
    for path in _hold_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            written.append(str(path))
        except Exception:
            continue
    return {"ok": bool(written), "written": written, "hold": payload}


def clear_desk_offline_hold(*, reason: str = "operator_clear") -> dict[str, Any]:
    removed: list[str] = []
    for path in _hold_paths():
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except Exception:
            continue
    return {"ok": True, "removed": removed, "reason": reason}


def is_desk_offline_hold_active() -> bool:
    for path in _hold_paths():
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict) and raw.get("active") is False:
                continue
            return True
        except Exception:
            # Corrupt hold still blocks auto-boot (fail closed).
            return True
    return False


def desk_offline_hold_snapshot() -> dict[str, Any]:
    active = is_desk_offline_hold_active()
    detail: dict[str, Any] = {}
    for path in _hold_paths():
        if not path.is_file():
            continue
        try:
            detail = json.loads(path.read_text(encoding="utf-8") or "{}")
            break
        except Exception:
            detail = {"path": str(path), "unreadable": True}
            break
    return {"active": active, "detail": detail}
