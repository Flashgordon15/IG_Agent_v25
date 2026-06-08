"""Label selection for walk-forward, S4 retrain, and trade learning."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _learning_config() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("learning") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def label_horizon() -> str:
    return str(_learning_config().get("label_horizon") or "6bar")


def label_keys_for_horizon(horizon: str | None = None) -> tuple[str, ...]:
    h = (horizon or label_horizon()).lower()
    if h in ("6", "6bar", "6-bar"):
        return ("label_6bar", "label_6", "label_3bar", "label_3")
    return ("label_3bar", "label_3", "label_6bar", "label_6")


def outcome_label(row: dict[str, Any], *, horizon: str | None = None) -> str:
    for key in label_keys_for_horizon(horizon):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).upper()
    return str(row.get("result") or "").upper()


def reset_label_config_cache_for_tests() -> None:
    _learning_config.cache_clear()
