"""Per-epic S2 momentum thresholds from config_v26 + tuned state snapshot."""

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
        _load_json(_project_root() / "config" / "config_v26.json").get("s2_momentum")
        or {}
    )


@lru_cache(maxsize=1)
def _tuned_snapshot() -> dict[str, Any]:
    return _load_json(
        _project_root() / "data_lake" / "state" / "s2_epic_thresholds.json"
    )


def reset_s2_config_cache_for_tests() -> None:
    _config_block.cache_clear()
    _tuned_snapshot.cache_clear()


def default_min_range_pct() -> float:
    return float(_config_block().get("default_min_range_pct") or 0.0008)


def get_s2_min_range_pct(epic: str) -> float:
    epic = str(epic or "").strip()
    tuned = (_tuned_snapshot().get("by_epic") or {}).get(epic) or {}
    if tuned.get("min_range_pct") is not None:
        return float(tuned["min_range_pct"])
    overrides = _config_block().get("epic_overrides") or {}
    if epic in overrides:
        return float(overrides[epic])
    return default_min_range_pct()
