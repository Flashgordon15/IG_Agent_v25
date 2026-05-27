"""Shared IG REST client — one login session per process for startup, stream, and trading."""

from __future__ import annotations

import threading
from typing import Any

from system.credentials_loader import Credentials
from system.engine_log import log_engine

_lock = threading.RLock()
_login_lock = threading.Lock()
_client: Any | None = None
_cred_key: tuple[str, str, str] | None = None


def _credential_key(credentials: Credentials) -> tuple[str, str, str]:
    return (
        credentials.ig_api_key,
        credentials.ig_account_id,
        credentials.account_type,
    )


def get_shared_rest_client(credentials: Credentials) -> Any:
    """Return the process-wide IGRestClient for these credentials."""
    global _client, _cred_key

    from ig_api.rest_client import IGRestClient

    key = _credential_key(credentials)
    with _lock:
        if _client is None or _cred_key != key:
            _client = IGRestClient(credentials)
            _cred_key = key
            log_engine("IG REST shared session: new client created")
        return _client


def _session_valid(client: Any) -> bool:
    session = getattr(client, "session", None)
    return bool(session and getattr(session, "is_valid", False))


def ensure_shared_authenticated(credentials: Credentials) -> Any:
    """Ensure shared client is logged in; returns the client."""
    client = get_shared_rest_client(credentials)
    if _session_valid(client):
        return client

    # Never hold the global client lock during IG network I/O — that wedges the Tk main
    # thread when Start DEMO / post-OK wiring runs concurrently with stream startup.
    with _login_lock:
        if _session_valid(client):
            return client
        client.login()
        if not _session_valid(client):
            raise RuntimeError(
                "IG shared REST session invalid after login — check credentials and API key"
            )
    return client


def clear_shared_rest_client() -> None:
    """Drop cached client (e.g. after credential change)."""
    global _client, _cred_key
    with _lock:
        _client = None
        _cred_key = None
