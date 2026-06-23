"""
High-fidelity local broker simulation when IG credentials are unconfigured.

Activated automatically during Gate 2 when credentials.json and shell env
keys are absent, so the API can bind and the boot pipeline continues in
sandbox mode without contacting IG.
"""

from __future__ import annotations

import os
from typing import Any

from ig_api.mock_clients import MockIGRest, MockRESTConfig
from system.engine_log import log_engine

_MOCK_ACTIVE = False
_DEFAULT_ACCOUNT_ID = "MOCK-V30-SANDBOX"
_PLACEHOLDER_KEYS = frozenset(
    {
        "",
        "your_api_key",
        "changeme",
        "mock-key",
        "mock",
        "placeholder",
    }
)


def mock_feed_active() -> bool:
    return _MOCK_ACTIVE or os.environ.get("IG_MOCK_FEED_ACTIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def mock_feed_forced() -> bool:
    return os.environ.get("IG_MOCK_FEED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def credentials_unconfigured(holder: Any) -> bool:
    """True when broker keys are missing or placeholder — safe for mock fallback."""
    if mock_feed_forced():
        return True
    creds = getattr(holder, "credentials", None)
    if creds is None:
        return True
    api_key = str(getattr(creds, "ig_api_key", "") or "").strip().lower()
    if api_key in _PLACEHOLDER_KEYS:
        return True
    username = str(getattr(creds, "ig_username", "") or "").strip().lower()
    if username in _PLACEHOLDER_KEYS or username == "mock-user":
        return True
    try:
        from system.env_loader import env_credentials_complete

        if not env_credentials_complete():
            from system.credentials_loader import credentials_path

            if not credentials_path().is_file():
                return True
    except Exception:
        pass
    return False


def should_use_mock_feed(holder: Any) -> bool:
    from system.agent_execution_mode import broker_demo_execution_required, production_execution_active

    if production_execution_active():
        return False
    if broker_demo_execution_required():
        return False
    return mock_feed_forced() or credentials_unconfigured(holder)


def activate_mock_feed_engine(
    *,
    account_id: str = _DEFAULT_ACCOUNT_ID,
    balance: float = 10_000.0,
) -> MockIGRest:
    """Start in-process mock REST + quote synthesis; marks process sandbox mode."""
    from system.guard.live_path_guard import block_mock_client_factory

    block_mock_client_factory("MockFeedEngine.activate_mock_feed_engine")
    global _MOCK_ACTIVE
    _MOCK_ACTIVE = True
    os.environ["IG_MOCK_FEED_ACTIVE"] = "1"
    os.environ.setdefault("IG_ALLOW_MOCK_TRADING", "1")

    client = MockIGRest(
        account_id=account_id,
        account_type="DEMO",
        mock_config=MockRESTConfig(balance=balance),
    )
    client.login()
    install_mock_shared_rest(client)
    try:
        from system.agent_execution_mode import force_market_open_active

        if force_market_open_active():
            start_aggressive_momentum_wave()
    except Exception:
        pass
    log_engine(
        "MockFeedEngine: local simulation active — IG broker bypassed "
        f"(account={account_id})"
    )
    return client


def install_mock_shared_rest(client: MockIGRest) -> None:
    """Point the shared REST session cache at the mock client."""
    import system.ig_rest_session as session_mod

    with session_mod._lock:
        session_mod._client = client
        session_mod._cred_key = (
            str(getattr(client, "api_key", "mock")),
            str(getattr(client, "account_id", _DEFAULT_ACCOUNT_ID)),
            str(getattr(client, "account_type", "DEMO")),
        )


def mock_hydration_detail(*, balance: float = 10_000.0) -> dict[str, Any]:
    return {
        "open_positions": 0,
        "working_orders": 0,
        "balance": balance,
        "available": balance,
        "profit_loss": 0.0,
        "mock_feed": True,
    }


def mock_account_verify(account_id: str = _DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
    return {
        "match": True,
        "mock_feed": True,
        "configured_account_id": account_id,
        "accounts": [{"account_id": account_id, "accountType": "SPREADBET"}],
    }


_MOMENTUM_EPICS = (
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
)
_MOMENTUM_VARIANCE_PCT = 0.025
_MOMENTUM_INTERVAL_SEC = 0.020
_momentum_armed = False


def start_aggressive_momentum_wave(
    *,
    variance_pct: float = _MOMENTUM_VARIANCE_PCT,
    interval_sec: float = _MOMENTUM_INTERVAL_SEC,
) -> None:
    """
    Enable Stream A aggressive random-walk (+/- variance) on Gold + Wall St.

    Only armed when at least one target epic has an open exchange session.
    """
    global _momentum_armed
    if _momentum_armed:
        return
    try:
        from system.market_integrity import epic_market_open

        if not any(epic_market_open(ep) for ep in _MOMENTUM_EPICS):
            log_engine(
                "MockFeedEngine: momentum wave skipped — all target markets CLOSED"
            )
            return
    except Exception:
        pass
    _momentum_armed = True
    os.environ["IG_MOMENTUM_WAVE"] = "1"
    os.environ["IG_MOMENTUM_VARIANCE_PCT"] = str(variance_pct)
    os.environ["IG_MOMENTUM_INTERVAL_SEC"] = str(interval_sec)
    try:
        from trading.multi_api_broker import ensure_multi_api_broker_started

        ensure_multi_api_broker_started()
        log_engine(
            f"MockFeedEngine: aggressive momentum wave armed "
            f"(+/- {variance_pct * 100:.1f}% every {interval_sec * 1000:.0f}ms) "
            f"epics={_MOMENTUM_EPICS}"
        )
    except Exception as exc:
        log_engine(
            f"MockFeedEngine: momentum wave broker start skipped: "
            f"{type(exc).__name__}: {exc}"
        )
