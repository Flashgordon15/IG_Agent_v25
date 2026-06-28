"""
GUI session attach protocol — discover and attach to the single active agent.

Read-only: never starts agents or modifies session locks.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

SESSION_IDENTITY_KEYS = (
    "app_mode",
    "account_scope",
    "session_id",
    "session_status",
    "data_root",
    "config_overlay",
    "engine_paths_armed",
    "port",
    "pid",
    "broker_plane",
    "api_port",
)

ZOMBIE_ATTACH_REASON = (
    "Session is ZOMBIE — run ./scripts/stop.sh then ./scripts/start.sh"
)
NO_SESSION_REASON = "No active agent session"


@dataclass(frozen=True)
class AttachResult:
    attached: bool
    reason: str
    session_identity: dict[str, Any] = field(default_factory=dict)


def extract_session_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull session identity fields from a /api/health or /api/gui_status payload."""
    identity: dict[str, Any] = {}
    for key in SESSION_IDENTITY_KEYS:
        if key in payload and payload[key] is not None:
            identity[key] = payload[key]
    if "port" not in identity and "api_port" in identity:
        identity["port"] = identity["api_port"]
    return identity


def evaluate_health_payload(payload: dict[str, Any] | None) -> AttachResult:
    """
    Decide attach eligibility from health/gui_status JSON.

    HEALTHY → attach; ZOMBIE → refuse with stop.sh guidance; else no session.
    """
    if not payload or not isinstance(payload, dict):
        return AttachResult(attached=False, reason=NO_SESSION_REASON)

    app_mode = str(payload.get("app_mode") or "").strip()
    if not app_mode:
        return AttachResult(attached=False, reason=NO_SESSION_REASON)

    status = str(payload.get("session_status") or "").upper()
    if status == "ZOMBIE":
        return AttachResult(attached=False, reason=ZOMBIE_ATTACH_REASON)

    if status != "HEALTHY":
        return AttachResult(attached=False, reason=NO_SESSION_REASON)

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return AttachResult(attached=False, reason=NO_SESSION_REASON)

    identity = extract_session_identity(payload)
    return AttachResult(
        attached=True,
        reason="attached",
        session_identity=identity,
    )


def fetch_health_payload(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    path: str = "/api/health",
    timeout_sec: float = 5.0,
) -> dict[str, Any] | None:
    """GET health/gui_status JSON — returns None when unreachable."""
    url = f"http://{host}:{int(port)}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-Agent-GUI-Attach/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def attach_to_session(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    timeout_sec: float = 5.0,
    health_path: str = "/api/health",
) -> AttachResult:
    """
    Query agent health and return attach decision.

    Never starts a trading agent or touches session locks.
    """
    payload = fetch_health_payload(host, port, path=health_path, timeout_sec=timeout_sec)
    return evaluate_health_payload(payload)


def attach_via_gui_status(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    timeout_sec: float = 5.0,
) -> AttachResult:
    """Attach using the dedicated /api/gui_status endpoint."""
    return attach_to_session(
        host,
        port,
        timeout_sec=timeout_sec,
        health_path="/api/gui_status",
    )
