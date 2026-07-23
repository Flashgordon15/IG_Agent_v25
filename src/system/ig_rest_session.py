"""Shared IG REST client — one login session per process for startup, stream, and trading."""

from __future__ import annotations

import threading
from typing import Any

from ig_api.exceptions import IGAuthError
from system.credentials_loader import Credentials
from system.engine_log import log_engine

_lock = threading.RLock()
_login_lock = threading.Lock()
_client: Any | None = None
_cred_key: tuple[str, str, str] | None = None


def _credential_key(credentials: Credentials) -> tuple[str, str, str]:
    from runtime.session_registry import resolve_process_account_id

    account_id = resolve_process_account_id(credentials)
    return (
        credentials.ig_api_key,
        account_id,
        credentials.account_type,
    )


def _use_session_registry() -> bool:
    try:
        from runtime.session_registry import registry_enabled

        return registry_enabled()
    except Exception:
        return bool(__import__("os").environ.get("IG_ACCOUNT_ID", "").strip())


def get_shared_rest_client(credentials: Credentials) -> Any:
    """Return the process-wide IGRestClient for these credentials."""
    global _client, _cred_key
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
            with _lock:
                if _client is not None:
                    return _client
            from system.testbed_firewall import testbed_panic

            testbed_panic("IG REST client construction blocked in HARDENED_TESTBED")
    except SystemExit:
        raise
    except Exception:
        pass

    if _use_session_registry():
        from runtime.session_registry import get_session_registry

        return get_session_registry().get_client_for_process(credentials)

    from ig_api.rest_client import IGRestClient

    key = _credential_key(credentials)
    with _lock:
        if _client is None or _cred_key != key:
            scoped = credentials
            account_id = key[1]
            if credentials.ig_account_id.strip().upper() != account_id:
                from dataclasses import replace

                scoped = replace(credentials, ig_account_id=account_id)
            _client = IGRestClient(scoped, account_id=account_id)
            _cred_key = key
            log_engine(
                f"IG REST shared session: new client created account={account_id}"
            )
        return _client


def _session_valid(client: Any) -> bool:
    session = getattr(client, "session", None)
    return bool(session and getattr(session, "is_valid", False))


def _proactive_refresh(client: Any) -> None:
    refresh = getattr(client, "proactive_refresh_if_needed", None)
    if callable(refresh):
        refresh()


def ensure_shared_authenticated(credentials: Credentials) -> Any:
    """Ensure shared client is logged in; returns the client."""
    client = get_shared_rest_client(credentials)
    if _session_valid(client):
        _proactive_refresh(client)
        return client

    # Never hold the global client lock during IG network I/O — that wedges the Tk main
    # thread when Start DEMO / post-OK wiring runs concurrently with stream startup.
    with _login_lock:
        if _session_valid(client):
            _proactive_refresh(client)
            return client
        try:
            client.login()
        except IGAuthError:
            log_engine("CRITICAL: IG authentication failed — check credentials")
            return client
        except Exception:
            log_engine("CRITICAL: IG authentication failed — check credentials")
            return client
        if not _session_valid(client):
            log_engine("CRITICAL: IG authentication failed — check credentials")
    return client


def clear_shared_rest_client() -> None:
    """Drop cached client (e.g. after credential change)."""
    global _client, _cred_key
    if _use_session_registry():
        try:
            from runtime.session_registry import get_session_registry
            from system.credentials_holder import get_credentials_holder

            holder = get_credentials_holder()
            if holder.credentials is not None:
                get_session_registry().clear_process_account(holder.credentials)
                return
        except Exception:
            pass
    with _lock:
        _client = None
        _cred_key = None


def evict_mock_shared_session() -> None:
    """Purge MockIGRest from the process-wide REST cache."""
    global _client, _cred_key
    try:
        from ig_api.mock_clients import MockIGRest
    except Exception:
        return
    if _use_session_registry():
        try:
            from runtime.session_registry import get_session_registry

            registry = get_session_registry()
            with registry._lock:  # noqa: SLF001 — scoped mock eviction
                for aid, client in list(registry._clients.items()):
                    if isinstance(client, MockIGRest):
                        registry.clear_account(aid)
        except Exception:
            pass
    with _lock:
        if isinstance(_client, MockIGRest):
            _client = None
            _cred_key = None
    try:
        import feeder.mock_feed_engine as mfe

        mfe._MOCK_ACTIVE = False  # noqa: SLF001
    except Exception:
        pass


def force_authenticated_ig_rest_client() -> Any:
    """
    Production override — always return a real IGRestClient (never MockIGRest).
    """
    from ig_api.mock_clients import MockIGRest
    from ig_api.rest_client import IG_DEMO_GATEWAY, IG_LIVE_GATEWAY, IGRestClient
    from system.credentials_holder import get_credentials_holder

    evict_mock_shared_session()
    holder = get_credentials_holder()
    credentials = holder.credentials
    if credentials is None:
        raise RuntimeError(
            "Authentic IG broker requires config/credentials/credentials.json"
        )
    client = ensure_shared_authenticated(credentials)
    if isinstance(client, MockIGRest):
        clear_shared_rest_client()
        client = ensure_shared_authenticated(credentials)
    if not isinstance(client, IGRestClient):
        raise RuntimeError(
            f"IG REST bind failed — expected IGRestClient, got {type(client).__name__}"
        )
    gateway = IG_DEMO_GATEWAY if credentials.account_type == "DEMO" else IG_LIVE_GATEWAY
    log_engine(
        f"IG DEMO EXECUTION: IGRestClient armed account={credentials.masked_account_id()} "
        f"type={credentials.account_type} gateway={gateway}"
    )
    return client


def shutdown_shared_ig_session() -> None:
    """Logout IG REST session and drop the shared client."""
    global _client
    with _lock:
        client = _client
    if client is None:
        return
    end = getattr(client, "end_session", None)
    if callable(end):
        try:
            end()
            log_engine("IG REST session ended")
        except Exception as e:
            log_engine(f"IG REST session end failed: {type(e).__name__}: {e}")
    clear_shared_rest_client()
