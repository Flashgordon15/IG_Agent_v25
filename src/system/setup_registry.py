"""
Setup registry — BAN / PROBE / ACTIVE from rolling expectancy (v26 Phase 1).

Live v25 reads ``src/data/state/setup_registry.json`` (written by shadow_compare
or nightly expectancy job). Never raises into the trading loop.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from system.paths import data_dir

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_mtime: float = 0.0
_CACHE_TTL_SEC = 30.0
_last_load_ts: float = 0.0


def registry_path() -> Path:
    return data_dir() / "state" / "setup_registry.json"


def _empty_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "",
        "rolling_days": 14,
        "enabled": False,
        "setups": {},
        "banned_keys": [],
    }


def load_registry(*, force: bool = False) -> dict[str, Any]:
    """Load registry with short TTL cache (safe on hot trading path)."""
    global _cached, _cached_mtime, _last_load_ts
    path = registry_path()
    now = time.time()
    with _lock:
        if not force and _cached is not None and (now - _last_load_ts) < _CACHE_TTL_SEC:
            return dict(_cached)
        if not path.is_file():
            _cached = _empty_registry()
            _cached_mtime = 0.0
            _last_load_ts = now
            return dict(_cached)
        try:
            mtime = path.stat().st_mtime
            if not force and _cached is not None and mtime == _cached_mtime:
                _last_load_ts = now
                return dict(_cached)
            raw = json.loads(path.read_text(encoding="utf-8"))
            _cached = raw if isinstance(raw, dict) else _empty_registry()
            _cached_mtime = mtime
            _last_load_ts = now
            return dict(_cached)
        except Exception:
            _cached = _empty_registry()
            _last_load_ts = now
            return dict(_cached)


def reset_registry_cache_for_tests() -> None:
    global _cached, _cached_mtime, _last_load_ts
    with _lock:
        _cached = None
        _cached_mtime = 0.0
        _last_load_ts = 0.0


def is_gate_enabled() -> bool:
    reg = load_registry()
    return bool(reg.get("enabled"))


def setup_status(setup_key: str) -> str:
    if not setup_key:
        return "INSUFFICIENT"
    reg = load_registry()
    setups = reg.get("setups") or {}
    if isinstance(setups, dict) and setup_key in setups:
        entry = setups[setup_key]
        if isinstance(entry, dict):
            return str(entry.get("status") or "INSUFFICIENT").upper()
    banned = reg.get("banned_keys") or []
    if setup_key in banned:
        return "BANNED"
    return "INSUFFICIENT"


def is_setup_banned(setup_key: str) -> bool:
    if not is_gate_enabled():
        return False
    return setup_status(setup_key) == "BANNED"


def write_registry_from_stats(
    setups: list[Any],
    *,
    rolling_days: int = 14,
    enabled: bool = True,
) -> Path:
    """Persist registry from v26 SetupStats list (expectancy engine)."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    setups_map: dict[str, dict[str, Any]] = {}
    banned: list[str] = []
    for s in setups:
        if hasattr(s, "setup_key"):
            sk = str(s.setup_key)
            status = str(s.status)
            row = {
                "status": status,
                "n": int(s.n),
                "wr": float(s.wr),
                "e_gbp": float(s.e_gbp),
                "total_pnl_gbp": float(s.total_pnl_gbp),
            }
        elif isinstance(s, dict):
            sk = str(s.get("setup_key") or "")
            status = str(s.get("status") or "INSUFFICIENT")
            row = {
                "status": status,
                "n": int(s.get("n") or 0),
                "wr": float(s.get("wr") or 0),
                "e_gbp": float(s.get("e_gbp") or 0),
                "total_pnl_gbp": float(s.get("total_pnl_gbp") or 0),
            }
        else:
            continue
        if not sk:
            continue
        setups_map[sk] = row
        if status == "BANNED":
            banned.append(sk)
    from datetime import datetime, timezone

    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rolling_days": rolling_days,
        "enabled": enabled,
        "setups": setups_map,
        "banned_keys": sorted(set(banned)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    load_registry(force=True)
    return path
