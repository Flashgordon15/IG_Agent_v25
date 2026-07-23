"""Halt source-of-truth — existence of a flag file must NOT pause entries.

Pause only when JSON payload has ``active: true`` (or equivalent truthy
``active``). Missing file / ``active: false`` / unreadable JSON = not halted.

Covers shared ``state/`` plus dual-port lane mirrors ``state_cfd/`` and
``state_sb/``, and ``deploy_hold.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HALT_FLAG_NAMES = (
    "entry_halt.json",
    "trading_paused.json",
    "offline_for_dev.json",
)
_DEPLOY_HOLD_NAME = "deploy_hold.json"


def flag_payload_active(raw: Any) -> bool:
    """True only when a parsed flag dict explicitly sets ``active`` truthy.

    Missing key / ``active: false`` / empty payload → not halted.
    """
    if not isinstance(raw, dict) or not raw:
        return False
    return bool(raw.get("active"))


def read_flag_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def flag_file_active(path: Path) -> bool:
    """Existence alone is never enough — require ``active: true``."""
    return flag_payload_active(read_flag_payload(path))


def _lane_state_roots() -> list[Path]:
    from system.paths import data_dir, state_dir

    root = Path(data_dir())
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in (state_dir(), root / "state", root / "state_cfd", root / "state_sb"):
        try:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def active_halt_flags(*, include_deploy_hold: bool = True) -> list[dict[str, Any]]:
    """Return every actively armed halt/hold flag across lane roots."""
    active: list[dict[str, Any]] = []
    names = list(_HALT_FLAG_NAMES)
    if include_deploy_hold:
        names.append(_DEPLOY_HOLD_NAME)
    for root in _lane_state_roots():
        for name in names:
            path = root / name
            payload = read_flag_payload(path)
            if not flag_payload_active(payload):
                continue
            active.append(
                {
                    "lane": root.name,
                    "name": name,
                    "path": str(path),
                    "reason": str(payload.get("reason") or name),
                    "active": True,
                }
            )
    return active


def any_entry_halt_active() -> bool:
    """True when entry_halt / trading_paused / offline_for_dev is actively armed."""
    for root in _lane_state_roots():
        for name in _HALT_FLAG_NAMES:
            if flag_file_active(root / name):
                return True
    return False


def any_deploy_hold_file_active() -> bool:
    for root in _lane_state_roots():
        if flag_file_active(root / _DEPLOY_HOLD_NAME):
            return True
    return False


def halt_status_snapshot() -> dict[str, Any]:
    flags = active_halt_flags(include_deploy_hold=True)
    by_name: dict[str, bool] = {
        "entry_halt": False,
        "trading_paused": False,
        "offline_for_dev": False,
        "deploy_hold": False,
    }
    for row in flags:
        key = str(row.get("name") or "").replace(".json", "")
        if key in by_name:
            by_name[key] = True
    return {
        "entries_halted": any(
            by_name[k] for k in ("entry_halt", "trading_paused", "offline_for_dev")
        ),
        "deploy_hold_active": bool(by_name["deploy_hold"]),
        "flags": by_name,
        "active_details": flags,
    }
