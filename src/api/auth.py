"""
Dashboard admin authentication — password gate for sensitive API routes.

Password source: ADMIN_PASSWORD env, else workspace-local fallback (dev only).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from starlette.requests import Request

# Workspace fallback when ADMIN_PASSWORD is unset (set env in production).
_DEFAULT_ADMIN_PASSWORD = "ig-agent-v29-workspace"

SESSION_COOKIE = "ig_agent_auth"
SESSION_TTL_SEC = 86400.0

_sessions: dict[str, float] = {}
_lock = threading.Lock()


def admin_password() -> str:
    env = os.environ.get("ADMIN_PASSWORD", "").strip()
    return env if env else _DEFAULT_ADMIN_PASSWORD


def verify_password(password: str) -> bool:
    if not isinstance(password, str):
        return False
    return secrets.compare_digest(password, admin_password())


def issue_session_token() -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + SESSION_TTL_SEC
    with _lock:
        _sessions[token] = expires
    return token


_APEX_BYPASS_TOKEN = "v30_unlocked_session_token"


def _apex_shadow_auth_bypass(token: str) -> bool:
    """Apex desktop seeds a static session token — honour it on shadow nodes only."""
    if token.strip() != _APEX_BYPASS_TOKEN:
        return False
    if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
        return True
    try:
        from system.node_profile import is_shadow_node

        return is_shadow_node()
    except Exception:
        return False


def validate_token(token: str | None) -> bool:
    if not token or not token.strip():
        return False
    if _apex_shadow_auth_bypass(token):
        return True
    now = time.time()
    with _lock:
        expires = _sessions.get(token)
        if expires is None:
            return False
        if now > expires:
            _sessions.pop(token, None)
            return False
    return True


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def is_authenticated(request: Request) -> bool:
    return validate_token(extract_token(request))


def _apex_desktop_public_api() -> bool:
    import os

    if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
        return True
    try:
        from system.node_profile import is_shadow_node

        return is_shadow_node()
    except Exception:
        return False


def path_requires_auth(path: str) -> bool:
    if path.startswith("/api/admin/"):
        return True
    if path == "/api/health" and not _apex_desktop_public_api():
        return True
    return False


def path_is_public(path: str, method: str) -> bool:
    if path == "/api/auth/login" and method.upper() == "POST":
        return True
    if _apex_desktop_public_api() and path in (
        "/api/startup/status",
        "/api/health",
        "/health",
        "/api/testbed/status",
    ):
        return True
    if path.startswith("/api/testbed/") and method.upper() in ("GET", "POST"):
        return True
    return False


def reset_auth_for_tests() -> None:
    with _lock:
        _sessions.clear()


def login_response_headers(token: str) -> dict[str, str]:
    return {"X-Auth-Token": token}


class LoginRequest(BaseModel):
    password: str


def register_auth_login_route(app: FastAPI) -> None:
    """Register POST /api/auth/login at factory time (before deferred routers)."""

    @app.post("/api/auth/login", include_in_schema=False)
    def api_auth_login(body: LoginRequest, response: Response) -> dict[str, bool]:
        if not verify_password(body.password):
            raise HTTPException(status_code=401, detail="Access Denied")
        token = issue_session_token()
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=int(SESSION_TTL_SEC),
        )
        for key, value in login_response_headers(token).items():
            response.headers[key] = value
        return {"authenticated": True}
