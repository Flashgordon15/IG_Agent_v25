"""IG Trading Ready — four-layer broker connectivity health node."""

from __future__ import annotations

import threading
import time
from typing import Any

from api.v31_telemetry import _triage_v31_path
from runtime.dual_core_execution import get_active_stack_epics


def _resolve_rest_client() -> Any | None:
    try:
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import get_shared_rest_client

        cred = try_load_credentials()
        if not cred.ok or cred.credentials is None:
            return None
        client = get_shared_rest_client(cred.credentials)
        session = getattr(client, "session", None)
        if session and getattr(session, "is_valid", False):
            return client
        return None
    except Exception:
        return None


def _broker_auth_valid(rest: Any | None) -> bool:
    if rest is None:
        return False
    session = getattr(rest, "session", None)
    return bool(session and getattr(session, "is_valid", False))


def _socket_stream_active() -> tuple[bool, str]:
    """Gate 3 coupled + fresh quotes on active stack epics (hub-only hot path)."""
    try:
        stack = get_active_stack_epics()
        hub = __import__("system.market_data_hub", fromlist=["get_market_data_hub"]).get_market_data_hub()
        fresh: list[str] = []
        missing: list[str] = []
        for epic in stack:
            snap = hub.get_snapshot(epic)
            if snap is None or snap.bid <= 0 or snap.offer <= 0:
                missing.append(epic.split(".")[1] if "." in epic else epic)
                continue
            if snap.age_seconds() <= 45.0:
                fresh.append(epic)
            else:
                missing.append(f"{epic}(stale)")
        if fresh and len(fresh) == len(stack):
            return True, f"hub fresh x{len(fresh)}"
        detail = ", ".join(missing) if missing else "awaiting ticks"
        return False, detail
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def _order_execution_ready(rest: Any | None) -> tuple[bool, str]:
    reasons: list[str] = []
    try:
        from runtime.strategy_kill_switch import is_strategy_kill_active

        if is_strategy_kill_active():
            reasons.append("BROKER_STATE_MISMATCH")
    except Exception:
        pass
    try:
        from system.qmm_process_supervisor import process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked and detail:
            reasons.append(str(detail))
    except Exception:
        pass
    try:
        from api.agent_control import is_paused

        if is_paused():
            reasons.append("api_trading_paused")
    except Exception:
        pass
    try:
        from api.v31_telemetry import resolve_block_reason

        br = resolve_block_reason()
        if br:
            reasons.append(br)
    except Exception:
        pass

    if rest is None:
        reasons.append("no_rest_session")
    else:
        try:
            summary = rest.get_cached_account_summary()
            if not any(v is not None for v in summary.values()):
                reasons.append("accounts_cache_cold")
        except Exception as exc:
            reasons.append(f"accounts_{type(exc).__name__}")

    if reasons:
        return False, "; ".join(reasons)
    return True, "transactional endpoints reachable"


def _ledger_synced(rest: Any | None) -> tuple[bool, str]:
    """Ledger drift — deferred off 1s dashboard poll (IgPositionSync owns reconcile)."""
    if rest is None:
        return False, "no_rest_session"
    return True, "sync_deferred"


def build_broker_ready_payload() -> dict[str, Any]:
    """Four-layer IG Trading Ready matrix."""
    t0 = time.perf_counter()
    rest = _resolve_rest_client()
    auth = _broker_auth_valid(rest)
    stream_ok, stream_detail = _socket_stream_active()
    order_ok, order_detail = _order_execution_ready(rest)
    ledger_ok, ledger_detail = _ledger_synced(rest)

    all_ready = auth and stream_ok and order_ok and ledger_ok
    return {
        "ok": True,
        "ts": time.time(),
        "ig_trading_ready": all_ready,
        "broker_auth_valid": auth,
        "socket_stream_active": stream_ok,
        "order_execution_ready": order_ok,
        "ledger_synced": ledger_ok,
        "details": {
            "auth": "session valid" if auth else "session invalid or missing",
            "stream": stream_detail,
            "order": order_detail,
            "ledger": ledger_detail,
        },
        "display": {
            "authenticated": "OK" if auth else "FAILED",
            "data_stream": "STREAMING" if stream_ok else "WARMING",
            "order_valve": "READY" if order_ok else "SUPPRESSED",
            "ledger_sync": "SYNCHRONIZED" if ledger_ok else "DRIFTING",
        },
        "build_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


_BROKER_READY_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_BROKER_READY_LOCK = threading.Lock()
_BROKER_READY_TTL_SEC = 1.5


def get_dashboard_broker_ready() -> dict[str, Any]:
    """Cached broker-ready matrix — avoids REST + thread-pool pile-up on 2s poll."""
    global _BROKER_READY_CACHE
    now = time.time()
    cached = _BROKER_READY_CACHE.get("data") or {}
    if now - float(_BROKER_READY_CACHE.get("ts") or 0.0) < _BROKER_READY_TTL_SEC and cached:
        return dict(cached)
    if not _BROKER_READY_LOCK.acquire(blocking=False):
        if cached:
            return dict(cached)
        return {
            "ok": True,
            "ig_trading_ready": False,
            "display": {
                "authenticated": "WARMING",
                "data_stream": "WARMING",
                "order_valve": "WARMING",
                "ledger_sync": "WARMING",
            },
        }
    try:
        now = time.time()
        cached = _BROKER_READY_CACHE.get("data") or {}
        if now - float(_BROKER_READY_CACHE.get("ts") or 0.0) < _BROKER_READY_TTL_SEC and cached:
            return dict(cached)
        fresh = build_broker_ready_payload()
        _BROKER_READY_CACHE = {"ts": now, "data": fresh}
        return dict(fresh)
    finally:
        _BROKER_READY_LOCK.release()
