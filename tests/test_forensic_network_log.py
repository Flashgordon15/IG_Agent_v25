"""Forensic network redaction + per-account token cache isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from system.forensic_network_log import (
    forensic_network_enabled,
    log_forensic_network,
    redact_headers,
    redact_json,
)


def test_redact_headers_never_emits_full_cst() -> None:
    secret_cst = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    secret_xst = "ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210"
    out = redact_headers(
        {
            "CST": secret_cst,
            "X-SECURITY-TOKEN": secret_xst,
            "X-IG-API-KEY": "my-api-key-value",
            "IG-ACCOUNT-ID": "Z6BAH4",
            "VERSION": "2",
        }
    )
    blob = json.dumps(out)
    assert secret_cst not in blob
    assert secret_xst not in blob
    assert "my-api-key-value" not in blob
    assert "Z6BAH4" in blob
    assert "present=True" in out["CST"] or "present=true" in out["CST"].lower()
    assert str(len(secret_cst)) in out["CST"]


def test_redact_json_strips_password_and_identifier() -> None:
    body = {
        "identifier": "trader@example.com",
        "password": "super-secret-password",
        "epic": "IX.D.DOW.IFM.IP",
        "size": 0.5,
        "direction": "BUY",
    }
    redacted = redact_json(body)
    assert redacted["epic"] == "IX.D.DOW.IFM.IP"
    assert redacted["size"] == 0.5
    assert redacted["password"]["present"] is True
    assert "super-secret-password" not in json.dumps(redacted)


def test_forensic_log_writes_redacted_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IG_FORENSIC_NETWORK", "1")
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IG_ENGINE_STATE_SUBDIR", "state_cfd")
    (tmp_path / "state_cfd").mkdir()

    log_forensic_network(
        account_id="Z6BAH4",
        method="POST",
        path="/positions/otc",
        headers={"CST": "FULLSECRETTOKENVALUE", "VERSION": "2"},
        request_json={
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "BUY",
            "size": 0.5,
            "password": "must-not-leak",
        },
        status_code=200,
        response_body='{"dealReference":"DIAAAA123","status":"ACCEPTED"}',
        source="test",
    )

    log_file = tmp_path / "state_cfd" / "forensic_network.log"
    assert log_file.is_file()
    raw = log_file.read_text(encoding="utf-8")
    assert "FULLSECRETTOKENVALUE" not in raw
    assert "must-not-leak" not in raw
    assert "DIAAAA123" in raw
    assert "Z6BAH4" in raw


def test_token_cache_paths_isolated_per_account() -> None:
    from dataclasses import replace

    from system.credentials_loader import Credentials

    creds = Credentials(
        ig_api_key="shared-key",
        ig_username="user",
        ig_password="pass",
        ig_account_type="DEMO",
        ig_account_id="Z6BAH4",
    )
    from ig_api.rest_client import IGRestClient

    cfd = IGRestClient(creds, account_id="Z6BAH4")
    sb = IGRestClient(
        replace(creds, ig_account_id="Z6BAH3"),
        account_id="Z6BAH3",
    )
    cfd_paths = {str(p) for p in cfd._token_cache_file_paths()}
    sb_paths = {str(p) for p in sb._token_cache_file_paths()}
    assert cfd_paths.isdisjoint(sb_paths)
    assert any("Z6BAH4" in p for p in cfd_paths)
    assert any("Z6BAH3" in p for p in sb_paths)
    assert cfd._auth is not sb._auth


def test_forensic_default_on_for_dual_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IG_FORENSIC_NETWORK", raising=False)
    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")
    assert forensic_network_enabled() is True
    monkeypatch.setenv("IG_FORENSIC_NETWORK", "0")
    assert forensic_network_enabled() is False


def test_session_registry_independent_auth_managers(monkeypatch) -> None:
    from runtime.session_registry import (
        get_session_registry,
        reset_session_registry_for_tests,
    )
    from system.credentials_loader import Credentials
    from system.engine_lane import DEFAULT_ACCOUNT_CFD, DEFAULT_ACCOUNT_SB

    reset_session_registry_for_tests()
    monkeypatch.setenv("IG_SESSION_REGISTRY", "1")

    created: list = []

    class _FakeAuth:
        def __init__(self) -> None:
            self._tokens = None

    class _FakeClient:
        def __init__(self, credentials, *, account_id=None, **kwargs) -> None:
            self.account_id = str(account_id).upper()
            self._auth = _FakeAuth()
            created.append(self)

        def proactive_refresh_if_needed(self) -> bool:
            return False

        def ensure_session(self) -> None:
            pass

    monkeypatch.setattr("ig_api.rest_client.IGRestClient", _FakeClient)

    base = Credentials(
        ig_api_key="key",
        ig_username="user",
        ig_password="pass",
        ig_account_type="DEMO",
        ig_account_id="Z6BAH4",
    )
    registry = get_session_registry()
    cfd = registry.get_client_for_account(DEFAULT_ACCOUNT_CFD, base)
    sb = registry.get_client_for_account(DEFAULT_ACCOUNT_SB, base)

    assert cfd._auth is not sb._auth
    assert cfd is not sb
    assert len(created) == 2
    reset_session_registry_for_tests()
