"""
Forward Flight Deck API reads to the trading agent (:8080) when the cockpit
web process is not co-located with the live agent interpreter.

When co-located, never HTTP-loopback — collect telemetry in-process instead.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from system.engine_log import log_engine

_PROXY_LOCK = threading.Lock()
_LAST_PROXY_AT: dict[str, float] = {}
_PROXY_MIN_INTERVAL_SEC = 5.0
_HYDRATE_COLLECT_MIN_SEC = 1.0
_last_hydrate_collect_at: float = 0.0
_last_hydrate_snapshot: dict[str, Any] | None = None


def agent_api_base() -> str:
    try:
        port = int(os.environ.get("IG_API_PORT", "8080"))
    except (TypeError, ValueError):
        port = 8080
    return f"http://127.0.0.1:{port}"


def in_trading_agent_process() -> bool:
    """True when this Python interpreter is hosting the live trading agent."""
    if os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1":
        return True
    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response()
        return bool(hl.get("agent_online"))
    except Exception:
        return False


def fetch_agent_json(path: str, *, timeout: float = 2.5) -> dict[str, Any] | None:
    """GET JSON from the trading agent API — never raises."""
    if in_trading_agent_process():
        return None
    route = str(path or "").strip()
    if not route.startswith("/"):
        route = f"/{route}"
    url = f"{agent_api_base()}{route}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log_engine(f"Flight Deck agent proxy {route}: {type(exc).__name__}")
        return None


def _rate_limited_proxy(path: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _PROXY_LOCK:
        last = _LAST_PROXY_AT.get(path, 0.0)
        if (now - last) < _PROXY_MIN_INTERVAL_SEC:
            return None
        _LAST_PROXY_AT[path] = now
    return fetch_agent_json(path, timeout=1.8)


def gates_all_pending(gates: Any) -> bool:
    if isinstance(gates, dict):
        if not gates:
            return True
        rows = list(gates.values())
    elif isinstance(gates, list):
        rows = gates
        if not rows:
            return True
    else:
        return True
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "pending").lower()
        if status not in ("pending", "", "unknown"):
            return False
    return True


def iron_cage_is_agent_coupled(
    local: dict[str, Any] | None,
    agent: dict[str, Any] | None = None,
) -> bool:
    """
    True when :8787 iron_cage reflects live agent state (not an orphan stub).

    Handoff invariant: ``trade_ready`` on either side always counts as coupled.
    When the agent snapshot is unavailable, prefer handoff over a stuck splash.
    """
    if not isinstance(local, dict):
        return True
    if local.get("trade_ready") is True:
        return True
    if not gates_all_pending(local.get("gates")):
        return True
    if not isinstance(agent, dict):
        return True
    if agent.get("trade_ready") is True:
        return True
    return not gates_all_pending(agent.get("gates"))


def iron_cage_needs_agent_proxy(local: dict[str, Any] | None) -> bool:
    if in_trading_agent_process():
        return False
    if not isinstance(local, dict):
        return True
    if local.get("trade_ready") is True:
        return False
    gates = local.get("gates")
    if not gates_all_pending(gates):
        return False
    blockers = local.get("blockers") or []
    if blockers == ["gates_incomplete"] or blockers == ["boot_not_ready"]:
        return True
    return gates_all_pending(gates)


def resolve_iron_cage_status() -> dict[str, Any]:
    """Prefer fast health_light-aligned snapshot — never block on full evaluate."""
    try:
        from system.iron_cage_readiness import fast_iron_cage_status_snapshot

        return fast_iron_cage_status_snapshot()
    except Exception:
        return {"ok": False, "trade_ready": False, "blockers": ["iron_cage_unavailable"]}


def resolve_ai_diagnostics() -> dict[str, Any]:
    try:
        from system.autonomic_healer import get_ai_diagnostics_snapshot

        local = get_ai_diagnostics_snapshot()
    except Exception:
        local = {}
    if in_trading_agent_process():
        return dict(local) if isinstance(local, dict) else {}
    if isinstance(local, dict) and (
        local.get("synthetic_hydration_active") is not None
        or local.get("cognitive_override_active") is not None
        or local.get("fallback_transport_tier")
        or local.get("transport_recovery")
    ):
        return dict(local)
    proxied = _rate_limited_proxy("/api/ai_diagnostics")
    if isinstance(proxied, dict) and proxied:
        return proxied
    return dict(local) if isinstance(local, dict) else {}


def gates_map_from_iron_cage(iron: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize iron_cage gate rows to the WS gate map shape."""
    out: dict[str, Any] = {}
    if not isinstance(iron, dict):
        return out
    gates = iron.get("gates")
    if isinstance(gates, list):
        for row in gates:
            if not isinstance(row, dict):
                continue
            gid = str(row.get("id") or "").upper()
            if gid:
                out[gid] = {
                    "status": str(row.get("status") or "pending").lower(),
                    "detail": str(row.get("detail") or ""),
                }
    elif isinstance(gates, dict):
        for gid, row in gates.items():
            key = str(gid or "").upper()
            if not key:
                continue
            if isinstance(row, dict):
                out[key] = {
                    "status": str(row.get("status") or "pending").lower(),
                    "detail": str(row.get("detail") or ""),
                }
            else:
                out[key] = {"status": str(row or "pending").lower(), "detail": ""}
    return out


def collect_live_telemetry_snapshot() -> dict[str, Any] | None:
    """In-process telemetry collect — hub quotes, spread, gates (no HTTP loopback)."""
    global _last_hydrate_collect_at, _last_hydrate_snapshot
    now = time.monotonic()
    with _PROXY_LOCK:
        if (
            _last_hydrate_snapshot
            and (now - _last_hydrate_collect_at) < _HYDRATE_COLLECT_MIN_SEC
        ):
            return dict(_last_hydrate_snapshot)
    try:
        from cockpit.telemetry_bridge import (
            DEFAULT_EPICS,
            bridge_is_active,
            collect_telemetry_snapshot,
            ensure_telemetry_bridge,
        )

        ensure_telemetry_bridge(epics=DEFAULT_EPICS)
        if not bridge_is_active():
            return None
        snap = collect_telemetry_snapshot()
        if not isinstance(snap, dict) or not snap:
            return None
        with _PROXY_LOCK:
            _last_hydrate_snapshot = dict(snap)
            _last_hydrate_collect_at = now
        return dict(snap)
    except Exception as exc:
        log_engine(f"Flight Deck live telemetry collect: {type(exc).__name__}: {exc}")
        return None


def hydrate_telemetry_from_agent(base: dict[str, Any] | None) -> dict[str, Any]:
    """Fill empty WS frames — direct collect when co-located, proxy only when isolated."""
    live = collect_live_telemetry_snapshot()
    if isinstance(live, dict) and live:
        out = dict(live)
        gates = out.get("gates") if isinstance(out.get("gates"), dict) else {}
        if gates_all_pending(gates):
            iron = resolve_iron_cage_status()
            merged = gates_map_from_iron_cage(iron)
            if merged:
                out["gates"] = merged
            if iron.get("ig_account_id"):
                out["ig_account_id"] = iron.get("ig_account_id")
            out["iron_cage"] = iron
        return out

    out = dict(base) if isinstance(base, dict) else {}
    gates = out.get("gates") if isinstance(out.get("gates"), dict) else {}
    if gates_all_pending(gates):
        iron = resolve_iron_cage_status()
        merged = gates_map_from_iron_cage(iron)
        if merged:
            out["gates"] = merged
        if iron.get("ig_account_id"):
            out["ig_account_id"] = iron.get("ig_account_id")
        out["iron_cage"] = iron
    if not out.get("epics") and not out.get("spread") and not in_trading_agent_process():
        gui = _rate_limited_proxy("/api/gui_status_fast") or _rate_limited_proxy("/api/health_light")
        if isinstance(gui, dict):
            feeds = gui.get("feeds") or gui.get("data_feeds")
            if isinstance(feeds, dict):
                out.setdefault("feeds", feeds)
    out.setdefault("ts", time.time())
    return out
