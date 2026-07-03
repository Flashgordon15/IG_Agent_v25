"""
Runtime tuning overlay — read/write signal and rotation parameters.

Writes to ``config/tuning_overlay.json`` only. Never modifies iron-cage hard
limits (max_daily_loss_gbp, REST budget caps).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_OVERLAY_PATH = Path(__file__).resolve().parents[2] / "config" / "tuning_overlay.json"

# Parameter schema: key -> (min, max, default)
_PARAM_BOUNDS: dict[str, tuple[float, float, float]] = {
    "z_score_entry_min": (-5.0, 0.0, -2.0),
    "z_score_entry_max": (0.0, 5.0, 2.0),
    "vol_filter_min_tpm": (0.0, 60.0, 3.0),
    "risk_per_trade_gbp": (1.0, 100.0, 40.0),
    "stop_distance_points": (5.0, 200.0, 45.0),
    "limit_distance_points": (5.0, 300.0, 60.0),
    "trailing_sensitivity": (0.1, 2.0, 1.0),
    "dynamic_limit_scale": (0.5, 3.0, 1.0),
    "rotation_weight_volatility": (0.0, 1.0, 0.35),
    "rotation_weight_spread": (0.0, 1.0, 0.20),
    "rotation_weight_feed": (0.0, 1.0, 0.25),
    "rotation_weight_pnl": (0.0, 1.0, 0.10),
    "rotation_weight_regime": (0.0, 1.0, 0.10),
}

_IRON_CAGE_FORBIDDEN_KEYS = frozenset(
    {
        "max_daily_loss_gbp",
        "max_daily_risk_loss",
        "rest_api_budget",
        "rest_budget",
        "max_rest_calls_per_min",
        "trade_ready",
        "iron_cage_override",
    }
)


def _defaults() -> dict[str, float]:
    return {k: v[2] for k, v in _PARAM_BOUNDS.items()}


def _overlay_path() -> Path:
    env = os.environ.get("IG_TUNING_OVERLAY", "").strip()
    return Path(env) if env else _OVERLAY_PATH


def _read_overlay_file() -> dict[str, Any]:
    path = _overlay_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_config_tuning() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        cfg = get_config().as_dict()
        block = cfg.get("tuning") or cfg.get("analytics_tuning") or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def get_tuning_params() -> dict[str, Any]:
    """Merged defaults + config + overlay file."""
    merged = _defaults()
    merged.update({k: float(v) for k, v in _read_config_tuning().items() if k in _PARAM_BOUNDS})
    overlay = _read_overlay_file()
    params_block = overlay.get("params") if isinstance(overlay.get("params"), dict) else overlay
    for key, val in (params_block or {}).items():
        if key in _PARAM_BOUNDS:
            try:
                merged[key] = float(val)
            except (TypeError, ValueError):
                pass
    return {
        "ok": True,
        "params": merged,
        "bounds": {k: {"min": v[0], "max": v[1], "default": v[2]} for k, v in _PARAM_BOUNDS.items()},
        "overlay_path": str(_overlay_path()),
        "updated_at": overlay.get("updated_at"),
        "ts": time.time(),
    }


def validate_tuning_update(payload: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    """Validate incoming params; return (cleaned, errors)."""
    errors: list[str] = []
    cleaned: dict[str, float] = {}
    if not isinstance(payload, dict):
        return {}, ["payload must be object"]
    for key in payload:
        if key in _IRON_CAGE_FORBIDDEN_KEYS:
            errors.append(f"forbidden_key:{key}")
    for key, raw in payload.items():
        if key not in _PARAM_BOUNDS:
            if key not in _IRON_CAGE_FORBIDDEN_KEYS:
                errors.append(f"unknown_key:{key}")
            continue
        lo, hi, _ = _PARAM_BOUNDS[key]
        try:
            val = float(raw)
        except (TypeError, ValueError):
            errors.append(f"invalid_number:{key}")
            continue
        if val < lo or val > hi:
            errors.append(f"out_of_bounds:{key} ({lo}..{hi})")
            continue
        cleaned[key] = val
    weights = [
        cleaned.get("rotation_weight_volatility"),
        cleaned.get("rotation_weight_spread"),
        cleaned.get("rotation_weight_feed"),
        cleaned.get("rotation_weight_pnl"),
        cleaned.get("rotation_weight_regime"),
    ]
    if any(w is not None for w in weights):
        present = [w for w in weights if w is not None]
        total = sum(present)
        if total > 0 and abs(total - 1.0) > 0.05:
            errors.append("rotation_weights_should_sum_to_1.0")
    return cleaned, errors


def apply_tuning_update(payload: dict[str, Any], *, source: str = "api") -> dict[str, Any]:
    """Persist overlay; does not force trade_ready or bypass iron cage."""
    cleaned, errors = validate_tuning_update(payload)
    if errors:
        return {"ok": False, "errors": errors, "applied": {}}
    if not cleaned:
        return {"ok": False, "errors": ["no_valid_params"], "applied": {}}

    with _lock:
        existing = _read_overlay_file()
        params_block = dict(existing.get("params") or existing)
        params_block.update(cleaned)
        body = {
            "version": 1,
            "source": source,
            "updated_at": time.time(),
            "params": params_block,
        }
        path = _overlay_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    return {
        "ok": True,
        "applied": cleaned,
        "params": get_tuning_params()["params"],
        "iron_cage_note": "tuning updates do not override trade_ready or hard risk caps",
    }


def reset_tuning_overlay_for_tests() -> None:
    path = _overlay_path()
    if path.is_file():
        path.unlink()
