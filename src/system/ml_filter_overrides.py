"""Apply synthetic-replay filter bounds from ml_model/meta.json at signal time."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

ML_RAMP_MIN_RECORDS = 100
ML_MIN_TRAINING_RECORDS = 500
BASELINE_MAX_RSI = 70.0

_lock = threading.RLock()
_cached: dict[str, Any] | None = None
_cached_mtime: float = 0.0
_init_logged: bool = False


def _meta_path() -> Path:
    return data_dir() / "ml_model" / "meta.json"


def _overrides_enabled() -> bool:
    try:
        from system.config_loader import get_config

        return bool(get_config().get("ml_filter_overrides_enabled", True))
    except Exception:
        return True


def training_record_count() -> int:
    """Effective ML training row count (live store + replay labels from training_meta)."""
    from data.ml_training_store import MLTrainingStore

    store = MLTrainingStore()
    live = store.record_count()
    training_meta_path = data_dir() / "ml_model" / "training_meta.json"
    try:
        if training_meta_path.is_file():
            meta = json.loads(training_meta_path.read_text(encoding="utf-8"))
            replay = int(meta.get("labelled_rows") or 0)
            return max(live, replay)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return live


def scale_max_rsi(strict_max_rsi: float, record_count: int) -> float:
    """
    Progressive linear ramp from safety ceiling to strict replay max_rsi.

    < 100 records: BASELINE_MAX_RSI (70.0).
    100–499: linear interpolation from 70.0 toward strict (when strict <= 70).
    >= 500: strict replay value unchanged.
    If strict > 70, hold baseline until full confidence at 500+ records.
    """
    try:
        strict = float(strict_max_rsi)
    except (TypeError, ValueError):
        return BASELINE_MAX_RSI
    if strict != strict:  # NaN guard
        return BASELINE_MAX_RSI
    try:
        records = int(record_count)
    except (TypeError, ValueError):
        records = 0
    if records < ML_RAMP_MIN_RECORDS:
        return BASELINE_MAX_RSI
    if records >= ML_MIN_TRAINING_RECORDS:
        return strict
    if strict > BASELINE_MAX_RSI:
        return BASELINE_MAX_RSI
    span = ML_MIN_TRAINING_RECORDS - ML_RAMP_MIN_RECORDS
    if span <= 0:
        return strict
    t = (records - ML_RAMP_MIN_RECORDS) / span
    return BASELINE_MAX_RSI + t * (strict - BASELINE_MAX_RSI)


def _progressive_max_rsi_mode(record_count: int) -> str:
    if record_count < ML_RAMP_MIN_RECORDS:
        return "baseline"
    if record_count >= ML_MIN_TRAINING_RECORDS:
        return "strict"
    return "ramp"


def _apply_progressive_scaling(raw: dict[str, Any]) -> dict[str, Any]:
    if not raw:
        return {}
    out = dict(raw)
    strict_raw = out.get("max_rsi")
    if strict_raw is None:
        return out
    try:
        strict = float(strict_raw)
    except (TypeError, ValueError):
        return out
    records = training_record_count()
    effective = scale_max_rsi(strict, records)
    out["max_rsi"] = effective
    _log_progressive_max_rsi(records, strict, effective)
    return out


def _log_progressive_max_rsi(records: int, strict: float, effective: float) -> None:
    global _init_logged
    if _init_logged:
        return
    _init_logged = True
    mode = _progressive_max_rsi_mode(records)
    log_engine(
        "ml_filter_overrides: max_rsi progressive scale "
        f"records={records} strict={strict:.2f} effective={effective:.2f} mode={mode}"
    )


def load_filter_overrides(*, force: bool = False) -> dict[str, Any]:
    global _cached, _cached_mtime
    if not _overrides_enabled():
        return {}
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
            base = overrides if isinstance(overrides, dict) else {}
            _cached = _apply_progressive_scaling(base)
            _cached_mtime = mtime
            return dict(_cached)
        except (json.JSONDecodeError, OSError):
            return dict(_cached or {})


def reset_filter_overrides_cache_for_tests() -> None:
    global _cached, _cached_mtime, _init_logged
    with _lock:
        _cached = None
        _cached_mtime = 0.0
        _init_logged = False


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
