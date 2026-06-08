"""Per-epic trailing-stop overrides from config_v26 + tuned replay snapshot."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


@lru_cache(maxsize=1)
def _config_block() -> dict[str, Any]:
    return (
        _load_json(_project_root() / "config" / "config_v26.json").get("trailing_stop")
        or {}
    )


@lru_cache(maxsize=1)
def _tuned_snapshot() -> dict[str, Any]:
    return _load_json(
        _project_root() / "data_lake" / "state" / "trail_epic_overrides.json"
    )


def reset_trail_config_cache_for_tests() -> None:
    _config_block.cache_clear()
    _tuned_snapshot.cache_clear()


def get_trail_overrides_for_epic(epic: str) -> dict[str, float]:
    """Return ATR multiples to merge into instrument trailing_stop config."""
    epic = str(epic or "").strip()
    out: dict[str, float] = {}

    tuned = (_tuned_snapshot().get("by_epic") or {}).get(epic) or {}
    for key in ("trail_trigger_atr_multiple", "trail_distance_atr_multiple"):
        if tuned.get(key) is not None:
            out[key] = float(tuned[key])

    cfg_overrides = _config_block().get("epic_overrides") or {}
    epic_cfg = cfg_overrides.get(epic) or {}
    if isinstance(epic_cfg, dict):
        for key in ("trail_trigger_atr_multiple", "trail_distance_atr_multiple"):
            if epic_cfg.get(key) is not None:
                out[key] = float(epic_cfg[key])

    return out
