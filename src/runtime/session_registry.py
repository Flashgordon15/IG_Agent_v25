"""Per-account IG REST session registry — v32 dual-port network isolation.

Each ``IGRestClient`` owns an independent :class:`~ig_api.auth.AuthManager`
(CST + X-SECURITY-TOKEN). Same API key with distinct ``IG-ACCOUNT-ID`` values
must **not** share on-disk token cache files — cache keys are suffixed by
``account_id`` (see ``IGRestClient._token_cache_file_paths``).

Proactive keep-alive runs per cached client (45-minute heartbeat) so one
account's refresh never demotes another's session tokens.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from typing import Any

from system.credentials_loader import Credentials
from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
)
from system.engine_log import log_engine

_ORIGIN_TO_ACCOUNT: dict[str, str] = {
    ENGINE_ORIGIN_CFD: DEFAULT_ACCOUNT_CFD,
    ENGINE_ORIGIN_SB: DEFAULT_ACCOUNT_SB,
}
_ACCOUNT_TO_ORIGIN: dict[str, str] = {
    DEFAULT_ACCOUNT_CFD: ENGINE_ORIGIN_CFD,
    DEFAULT_ACCOUNT_SB: ENGINE_ORIGIN_SB,
}

_registry_lock = threading.RLock()
_registry: AccountSessionRegistry | None = None
_KEEPALIVE_INTERVAL_SEC = 45 * 60


def registry_enabled() -> bool:
    """True when per-account REST clients must not clobber each other."""
    return (
        os.environ.get("IG_SESSION_REGISTRY", "").strip() == "1"
        or os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
        or bool(os.environ.get("IG_ACCOUNT_ID", "").strip())
    )


def resolve_process_account_id(credentials: Credentials) -> str:
    """Process-scoped account — CLI ``IG_ACCOUNT_ID`` wins over credentials file."""
    env_aid = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
    if env_aid:
        return env_aid
    return str(credentials.ig_account_id or "").strip().upper()


def credentials_for_account(credentials: Credentials, account_id: str) -> Credentials:
    """Same API key / login; distinct ``ig_account_id`` for IG-ACCOUNT-ID headers."""
    aid = str(account_id or "").strip().upper()
    if not aid:
        return credentials
    if credentials.ig_account_id.strip().upper() == aid:
        return credentials
    return replace(credentials, ig_account_id=aid)


class AccountSessionRegistry:
    """
    In-process cache of independent ``IGRestClient`` instances per IG account.

    Client A (CFD / QUANT_SNIPER / Z6BAH4) and Client B (SB / MACRO_SENTINEL /
    Z6BAH3) share one API key but must never share session state or token eviction.
    Each dual-port process binds one account via ``IG_ACCOUNT_ID``; the registry
    hardens lookup and prevents cross-account clobber when code paths re-enter.
    """

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._cred_keys: dict[str, tuple[str, str, str]] = {}
        self._keepalive_started: set[str] = set()
        self._lock = threading.RLock()

    def _ensure_keepalive(self, account_id: str, client: Any) -> None:
        """One daemon refresh loop per account client — independent CST lifecycle."""
        aid = str(account_id or "").strip().upper()
        if not aid:
            return
        with self._lock:
            if aid in self._keepalive_started:
                return
            self._keepalive_started.add(aid)

        def _loop() -> None:
            while True:
                time.sleep(_KEEPALIVE_INTERVAL_SEC)
                try:
                    with self._lock:
                        live = self._clients.get(aid)
                    if live is not client:
                        return
                    refresh = getattr(client, "proactive_refresh_if_needed", None)
                    if callable(refresh):
                        if refresh():
                            log_engine(
                                f"IG session registry keep-alive: refreshed account={aid}"
                            )
                            continue
                    ensure = getattr(client, "ensure_session", None)
                    if callable(ensure):
                        ensure()
                        log_engine(
                            f"IG session registry keep-alive: verified account={aid}"
                        )
                except Exception as exc:
                    log_engine(
                        f"IG session registry keep-alive account={aid} "
                        f"failed: {type(exc).__name__}: {exc}"
                    )

        threading.Thread(
            target=_loop,
            name=f"ig-keepalive-{aid}",
            daemon=True,
        ).start()
        log_engine(
            f"IG session registry keep-alive armed account={aid} "
            f"interval={_KEEPALIVE_INTERVAL_SEC // 60}m"
        )

    @staticmethod
    def _credential_key(credentials: Credentials, account_id: str) -> tuple[str, str, str]:
        return (
            credentials.ig_api_key,
            str(account_id).strip().upper(),
            credentials.account_type,
        )

    def get_client_for_account(
        self,
        account_id: str,
        credentials: Credentials,
    ) -> Any:
        from ig_api.rest_client import IGRestClient

        aid = str(account_id or "").strip().upper()
        if not aid:
            raise ValueError("session_registry: account_id required")
        scoped = credentials_for_account(credentials, aid)
        key = self._credential_key(scoped, aid)
        with self._lock:
            existing = self._clients.get(aid)
            if existing is not None and self._cred_keys.get(aid) == key:
                self._ensure_keepalive(aid, existing)
                return existing
            client = IGRestClient(scoped, account_id=aid)
            self._clients[aid] = client
            self._cred_keys[aid] = key
            log_engine(
                f"IG session registry: client bound account={aid} "
                f"origin={_ACCOUNT_TO_ORIGIN.get(aid, 'unknown')} "
                f"(independent AuthManager + token cache key)"
            )
            self._ensure_keepalive(aid, client)
            return client

    def get_client_for_engine(
        self,
        origin: str,
        credentials: Credentials,
    ) -> Any:
        origin_u = str(origin or "").strip().upper()
        aid = _ORIGIN_TO_ACCOUNT.get(origin_u)
        if not aid:
            raise ValueError(
                f"session_registry: unknown engine origin {origin_u!r} "
                f"(expected {ENGINE_ORIGIN_CFD} or {ENGINE_ORIGIN_SB})"
            )
        return self.get_client_for_account(aid, credentials)

    def get_client_for_process(self, credentials: Credentials) -> Any:
        """Return the REST client for this process's ``IG_ACCOUNT_ID`` binding."""
        aid = resolve_process_account_id(credentials)
        origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
        if origin in _ORIGIN_TO_ACCOUNT:
            expected = _ORIGIN_TO_ACCOUNT[origin]
            if aid and aid != expected:
                log_engine(
                    f"IG session registry: IG_ACCOUNT_ID={aid} != "
                    f"origin default {expected} — using {aid}"
                )
        return self.get_client_for_account(aid, credentials)

    def clear_account(self, account_id: str) -> None:
        aid = str(account_id or "").strip().upper()
        with self._lock:
            self._clients.pop(aid, None)
            self._cred_keys.pop(aid, None)
            self._keepalive_started.discard(aid)

    def clear_process_account(self, credentials: Credentials) -> None:
        self.clear_account(resolve_process_account_id(credentials))

    def clear_all(self) -> None:
        with self._lock:
            self._clients.clear()
            self._cred_keys.clear()
            self._keepalive_started.clear()


def get_session_registry() -> AccountSessionRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = AccountSessionRegistry()
        return _registry


def reset_session_registry_for_tests() -> None:
    """Test helper — drop cached clients and registry singleton."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.clear_all()
        _registry = None
