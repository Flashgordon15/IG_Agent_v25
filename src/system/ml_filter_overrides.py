"""Apply synthetic-replay filter bounds from ml_model/meta.json at signal time."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from system.paths import data_dir

_lock = threading.RLock()
_cached: dict[str, Any] | None = None
_cached_mtime: float = 0.0


def _meta_path() -> Path:
    return data_dir() / "ml_model" / "meta.json"


def load_filter_overrides(*, force: bool = False) -> dict[str, Any]:
    global _cached, _cached_mtime
    path = _meta_path()
    with _lock:
        if not path.is_file():
            _cached = {}
            _cached_mtime = 0.0
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return dict(_cached or {})
        if not force and _cached is not None and mtime == _cached_mtime:
            return dict(_cached)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            overrides = raw.get("filter_overrides") if isinstance(raw, dict) else {}
            _cached = overrides if isinstance(overrides, dict) else {}
            _cached_mtime = mtime
            return dict(_cached)
        except (json.JSONDecodeError, OSError):
            return dict(_cached or {})


def reset_filter_overrides_cache_for_tests() -> None:
    global _cached, _cached_mtime
    with _lock:
        _cached = None
        _cached_mtime = 0.0


def evaluate_filter_block(
    *,
    adjusted_score: float,
    raw_score: float,
    rsi: float,
    atr_ratio: float,
) -> tuple[bool, str]:
    """Return (blocked, reason). Only blocks when meta filter bounds are violated."""
    bounds = load_filter_overrides()
    if not bounds:
        return False, ""

    def _check(label: str, value: float, key: str, op: str) -> tuple[bool, str]:
        raw = bounds.get(key)
        if raw is None:
            return False, ""
        try:
            limit = float(raw)
        except (TypeError, ValueError):
            return False, ""
        if op == "max" and value > limit:
            return True, f"ml filter {key}: {value:.2f} > {limit:.2f}"
        if op == "min" and value < limit:
            return True, f"ml filter {key}: {value:.2f} < {limit:.2f}"
        return False, ""

    for label, value, key, op in (
        ("adjusted_score", adjusted_score, "max_adjusted_score", "max"),
        ("adjusted_score", adjusted_score, "min_adjusted_score", "min"),
        ("raw_score", raw_score, "max_raw_score", "max"),
        ("raw_score", raw_score, "min_raw_score", "min"),
        ("rsi", rsi, "max_rsi", "max"),
        ("rsi", rsi, "min_rsi", "min"),
        ("atr_ratio", atr_ratio, "max_atr_ratio", "max"),
        ("atr_ratio", atr_ratio, "min_atr_ratio", "min"),
    ):
        blocked, reason = _check(label, value, key, op)
        if blocked:
            return True, reason
    return False, ""
