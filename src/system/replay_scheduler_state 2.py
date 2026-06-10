"""Shared replay scheduler state — one JSON file for script, API, and UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file

STATE_PATH = data_dir() / "replay_scheduler_state.json"
_LEGACY_STATE_PATH = data_dir() / "replay_state.json"


def load_replay_scheduler_state() -> dict[str, Any]:
    """Load scheduler state, migrating legacy replay_state.json if needed."""
    if STATE_PATH.is_file():
        data = read_json_file(STATE_PATH)
        if isinstance(data, dict):
            return _normalize_state(data)
    if _LEGACY_STATE_PATH.is_file():
        try:
            legacy = json.loads(_LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            legacy = {}
        if isinstance(legacy, dict) and legacy:
            merged = _normalize_state(_from_legacy(legacy))
            save_replay_scheduler_state(merged)
            return merged
    return {}


def save_replay_scheduler_state(state: dict[str, Any]) -> None:
    atomic_write_json(STATE_PATH, _normalize_state(state))


def _from_legacy(legacy: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_run_time": legacy.get("last_replay_timestamp") or legacy.get("last_run"),
        "bars_processed": legacy.get("bar_count") or legacy.get("bars_cache"),
        "calibration_factor": legacy.get("calibration_factor"),
        "results_rows": legacy.get("results_rows"),
        "status": legacy.get("status", "idle"),
    }


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(raw)
    if out.get("last_run_time") is None and out.get("last_run") is not None:
        out["last_run_time"] = out["last_run"]
    if out.get("bars_processed") is None:
        out["bars_processed"] = out.get("bar_count") or out.get("bars_cache")
    if out.get("status") not in ("idle", "running", "failed"):
        out["status"] = "idle"
    return out
