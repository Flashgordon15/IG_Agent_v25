"""
Session identity fields for /api/health — APP_MODE contract observability.

Lifecycle-only; does not touch trading logic.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_IDENTITY_CACHE: dict[str, Any] = {}
_IDENTITY_CACHE_AT: float = 0.0
_IDENTITY_CACHE_TTL_SEC = 8.0
_IDENTITY_LOCK = threading.Lock()

from runtime.app_mode import AppMode, broker_plane_for, resolve_app_mode, resolve_data_root
from runtime.session_lock import (
    TESTBED_ACCOUNT_SCOPE,
    lock_path_for_scope,
    mask_account_scope,
    read_session_lock,
    resolve_account_scope,
    session_status_for_record,
)


def make_session_id(pid: int | None = None, started_at: int | None = None) -> str:
    """Canonical session id: ``{pid}-{started_at_epoch}``."""
    p = int(pid if pid is not None else os.getpid())
    ts = int(started_at if started_at is not None else time.time())
    return f"{p}-{ts}"


def _engine_paths_armed() -> dict[str, bool]:
    path_a = False
    path_b = False
    micro = False
    try:
        from api.agent_control import is_trading_running

        path_a = bool(is_trading_running())
    except Exception:
        pass
    try:
        from runtime.trade_manager import get_dual_core_coordinator

        coord = get_dual_core_coordinator()
        if coord is not None:
            path_b = True
            micro = True
    except Exception:
        pass
    try:
        from system.system_state import get_system_state

        phase = str(get_system_state().snapshot().get("phase") or "").upper()
        if phase == "G5" and not path_a:
            path_a = True
    except Exception:
        pass
    return {"path_a": path_a, "path_b": path_b, "micro": micro}


def _read_own_session_record() -> dict[str, Any] | None:
    try:
        mode = resolve_app_mode()
        scope = resolve_account_scope(mode)
        root = resolve_data_root(mode)
        path = lock_path_for_scope(scope, root)
        record = read_session_lock(path)
        if not record:
            return None
        holder = int(record.get("pid") or 0)
        if holder in (0, os.getpid()):
            return record
    except Exception:
        pass
    return None


def _build_session_identity_fields_uncached() -> dict[str, Any]:
    """Uncached implementation — called by the TTL cache wrapper."""
    try:
        mode = resolve_app_mode()
    except ValueError:
        return {}

    scope_raw = os.environ.get("IG_ACCOUNT_SCOPE", "").strip()
    if not scope_raw:
        try:
            scope_raw = resolve_account_scope(mode)
        except Exception:
            scope_raw = TESTBED_ACCOUNT_SCOPE if mode is AppMode.TESTBED else ""

    data_root = os.environ.get("IG_DATA_ROOT", "").strip() or resolve_data_root(mode)
    config_overlay = os.environ.get("IG_AGENT_CONFIG", "").strip()
    port_raw = os.environ.get("IG_API_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw.isdigit() else 8080
    except (TypeError, ValueError):
        port = 8080

    record = _read_own_session_record()
    session_id = ""
    session_status = ""
    if record:
        session_id = str(record.get("session_id") or "")
        if not session_id:
            session_id = make_session_id(
                int(record.get("pid") or os.getpid()),
                record.get("started_at"),
            )
        session_status = session_status_for_record(record)

    paths_armed = _engine_paths_armed()
    return {
        "app_mode": mode.value,
        "broker_plane": broker_plane_for(mode),
        "account_scope": mask_account_scope(scope_raw) if scope_raw else "",
        "session_id": session_id,
        "session_status": session_status,
        "data_root": data_root,
        "config_overlay": config_overlay,
        "engine_paths_armed": paths_armed,
        "paths_armed": dict(paths_armed),
        "api_port": port,
        "port": port,
        "pid": os.getpid(),
    }


def build_session_identity_fields() -> dict[str, Any]:
    """Fields merged into /api/health — TTL-cached to avoid repeated health_endpoint_ok calls."""
    global _IDENTITY_CACHE, _IDENTITY_CACHE_AT
    now = time.monotonic()
    with _IDENTITY_LOCK:
        if _IDENTITY_CACHE and (now - _IDENTITY_CACHE_AT) < _IDENTITY_CACHE_TTL_SEC:
            return dict(_IDENTITY_CACHE)
    result = _build_session_identity_fields_uncached()
    with _IDENTITY_LOCK:
        _IDENTITY_CACHE = result
        _IDENTITY_CACHE_AT = now
    return dict(result)


def reset_session_identity_cache_for_tests() -> None:
    global _IDENTITY_CACHE, _IDENTITY_CACHE_AT
    with _IDENTITY_LOCK:
        _IDENTITY_CACHE = {}
        _IDENTITY_CACHE_AT = 0.0
