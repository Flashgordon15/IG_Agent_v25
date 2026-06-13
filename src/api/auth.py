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


def validate_token(token: str | None) -> bool:
    if not token or not token.strip():
        return False
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


def path_requires_auth(path: str) -> bool:
    if path.startswith("/api/admin/"):
        return True
    return path == "/api/health"


def path_is_public(path: str, method: str) -> bool:
    if path == "/api/auth/login" and method.upper() == "POST":
        return True
    return False


def reset_auth_for_tests() -> None:
    with _lock:
        _sessions.clear()


def login_response_headers(token: str) -> dict[str, str]:
    return {"X-Auth-Token": token}
