"""Track per-epic OHLC bootstrap readiness for health and supervision."""

from __future__ import annotations

import json
from typing import Any

from system.paths import data_dir
from trading.ohlc_bootstrap import MIN_CACHE_BARS_FOR_BOOTSTRAP, local_cache_ready

_STATE_PATH = data_dir() / "state" / "ohlc_readiness.json"
_bootstrap: dict[str, dict[str, Any]] = {}


def record_bootstrap(epic: str, market: str, bars_injected: int) -> None:
    """Record bars injected during startup OHLC bootstrap."""
    key = str(epic or "").strip()
    if not key:
        return
    bars = int(bars_injected)
    _bootstrap[key] = {
        "market": str(market or ""),
        "bars_injected": bars,
        "ready": bars >= MIN_CACHE_BARS_FOR_BOOTSTRAP,
    }


def finalize_bootstrap_state() -> dict[str, Any]:
    """Persist bootstrap results after parallel OHLC seeding."""
    not_ready = [epic for epic, row in _bootstrap.items() if not row.get("ready")]
    payload: dict[str, Any] = {
        "epics": dict(_bootstrap),
        "not_ready": not_ready,
        "ready_count": sum(1 for row in _bootstrap.values() if row.get("ready")),
    }
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
    return payload


def load_bootstrap_state() -> dict[str, Any]:
    """Read persisted OHLC readiness (best-effort)."""
    try:
        if _STATE_PATH.is_file():
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def is_epic_ohlc_ready(epic: str, *, market: str = "") -> bool:
    """True when bootstrap injected enough bars or local cache is warm."""
    key = str(epic or "").strip()
    if not key:
        return False
    entry = _bootstrap.get(key)
    if entry is not None:
        return bool(entry.get("ready"))
    persisted = (load_bootstrap_state().get("epics") or {}).get(key)
    if isinstance(persisted, dict) and "ready" in persisted:
        return bool(persisted.get("ready"))
    return local_cache_ready(key, market)


def epic_quote_health_exempt(epic: str) -> bool:
    """Exempt stale quote checks when OHLC never bootstrapped."""
    return not is_epic_ohlc_ready(epic)


def reset_bootstrap_state_for_tests() -> None:
    """Clear in-memory bootstrap map (tests only)."""
    _bootstrap.clear()
