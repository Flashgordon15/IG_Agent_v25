"""
Shadow Brain → Live Vanguard tolerance handoff.

Publishes gate-floor adjustments computed on the shadow track for consumption
by the live (:8080) execution plane without blocking the hot path.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, project_root

_LOCK = threading.Lock()
_LAST_PUBLISHED: dict[str, Any] = {}


def tolerance_manifest_path() -> Path:
    canonical = project_root() / "src" / "data" / "live_tolerance_manifest.json"
    runtime = data_dir() / "live_tolerance_manifest.json"
    return runtime if runtime.is_file() else canonical


def publish_live_tolerance(
    adjustments: dict[str, Any],
    *,
    epic: str = "",
    market: str = "",
    near_miss_gate: str = "",
    margin_pct: float = 0.0,
) -> dict[str, Any]:
    """Write tolerance payload for live track pickup."""
    global _LAST_PUBLISHED

    payload = {
        "published_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "published_at_epoch": time.time(),
        "source": "shadow_brain",
        "epic": str(epic or ""),
        "market": str(market or ""),
        "near_miss_gate": str(near_miss_gate or ""),
        "margin_pct": round(float(margin_pct), 3),
        "adjustments": dict(adjustments or {}),
        "live_floors": dict(adjustments.get("live_floors") or adjustments),
    }
    with _LOCK:
        _LAST_PUBLISHED = dict(payload)
        path = tolerance_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        runtime = data_dir() / "live_tolerance_manifest.json"
        if runtime != path:
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log_engine(
        "LiveToleranceBridge: published "
        f"gate={near_miss_gate or '—'} margin={margin_pct:.2f}% "
        f"keys={list((adjustments.get('live_floors') or adjustments).keys())}"
    )
    return payload


def load_live_tolerance_manifest() -> dict[str, Any] | None:
    path = tolerance_manifest_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _merge_tolerance_floors(floors: dict[str, Any]) -> dict[str, Any]:
    from system.config_loader import get_config

    cfg = get_config()
    data = dict(cfg.as_dict())
    applied: dict[str, Any] = {}
    if "signal_threshold_floor" in floors:
        val = float(floors["signal_threshold_floor"])
        prot = dict(data.get("protective_learning") or {})
        prot["signal_threshold_floor"] = val
        data["protective_learning"] = prot
        applied["signal_threshold_floor"] = val
    if "fitness_min_floor" in floors:
        val = float(floors["fitness_min_floor"])
        prot = dict(data.get("protective_learning") or {})
        prot["fitness_min_floor"] = val
        data["protective_learning"] = prot
        soak = dict(data.get("demo_soak_mode") or {})
        soak["fitness_min"] = val
        data["demo_soak_mode"] = soak
        applied["fitness_min_floor"] = val
    if "ml_veto_min_probability" in floors:
        val = float(floors["ml_veto_min_probability"])
        ml = dict(data.get("ml_veto") or {})
        ml["min_probability"] = val
        data["ml_veto"] = ml
        applied["ml_veto_min_probability"] = val
    if not applied:
        return {}
    cfg._data.clear()
    cfg._data.update(data)
    log_engine(f"LiveToleranceBridge: applied live floors {applied}")
    return applied


def apply_tolerance_payload(payload: dict[str, Any]) -> bool:
    """Live track — apply gate-floor adjustments from HTTP or manifest body."""
    floors = payload.get("live_floors") or payload.get("adjustments") or {}
    if not isinstance(floors, dict) or not floors:
        return False
    try:
        return bool(_merge_tolerance_floors(floors))
    except Exception as exc:
        log_guarded_exception("live_tolerance_bridge", exc)
        return False


def apply_live_tolerance_if_pending() -> bool:
    """Live track — merge pending shadow brain floors into runtime config overlay."""
    manifest = load_live_tolerance_manifest()
    if manifest is None:
        return False
    published = float(manifest.get("published_at_epoch") or 0)
    if published <= 0:
        return False
    return apply_tolerance_payload(manifest)


def brain_telemetry_snapshot() -> dict[str, Any]:
    with _LOCK:
        published = dict(_LAST_PUBLISHED)
    manifest = load_live_tolerance_manifest()
    funnel: dict[str, Any] = {}
    try:
        from trading.gate_funnel_counter import read_funnel_snapshot

        funnel = read_funnel_snapshot()
    except Exception:
        pass
    return {
        "published": published,
        "manifest": manifest,
        "funnel": funnel,
        "manifest_path": str(tolerance_manifest_path()),
    }
