"""IG REST token age tracking, proactive refresh, and 401 re-login (Fix 4)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ig_api.auth import SessionTokens
from ig_api.exceptions import IGAuthError
from ig_api.rest_client import IGRestClient
from system.credentials_loader import Credentials
from system import engine_log


def _credentials() -> Credentials:
    return Credentials(
        ig_api_key="key",
        ig_username="user",
        ig_password="pass",
        ig_account_type="DEMO",
        ig_account_id="ACC1",
    )


def _client_with_tokens(*, token_age: float = 0.0) -> IGRestClient:
    client = IGRestClient(_credentials())
    client._auth._tokens = SessionTokens(
        cst="cst",
        security_token="xst",
        account_id="ACC1",
    )
    client._touch_token_created()
    if token_age > 0:
        client._token_created_at = time.time() - token_age
    return client


class TokenRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        engine_log._LOG.parent.mkdir(parents=True, exist_ok=True)
        engine_log._LOG.write_text("", encoding="utf-8")

    def test_proactive_refresh_when_token_older_than_five_hours(self) -> None:
        client = _client_with_tokens(token_age=IGRestClient.TOKEN_MAX_AGE_SECONDS + 60)
        with patch.object(client, "_refresh_session_tokens", return_value=True) as refresh:
            self.assertTrue(client.proactive_refresh_if_needed())
            refresh.assert_called_once()
        log_text = engine_log._LOG.read_text(encoding="utf-8")
        self.assertIn("IG session refreshed — tokens renewed after", log_text)

    def test_401_triggers_relogin_and_retries_once(self) -> None:
        client = _client_with_tokens()
        ok = requests.Response()
        ok.status_code = 200
        ok._content = b"{}"
        unauthorized = requests.Response()
        unauthorized.status_code = 401
        unauthorized._content = b'{"errorCode":"error.security.invalid-details"}'

        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
            patch("system.rest_api_budget.get_rest_api_budget") as budget,
            patch.object(client._session, "request", side_effect=[unauthorized, ok]) as req,
            patch.object(client, "_safe_relogin", return_value=True) as relogin,
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            mgr.return_value.handle_http_response.return_value = None
            budget.return_value.acquire.return_value = None
            r = client.request("GET", "/positions", headers=client._auth_headers("2"))

        self.assertEqual(r.status_code, 200)
        relogin.assert_called_once()
        self.assertEqual(req.call_count, 2)

    def test_relogin_failure_logs_critical_and_returns_safe_401(self) -> None:
        client = _client_with_tokens()
        unauthorized = requests.Response()
        unauthorized.status_code = 401
        unauthorized._content = b"{}"

        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
            patch("system.rest_api_budget.get_rest_api_budget") as budget,
            patch.object(client._session, "request", return_value=unauthorized),
            patch.object(client, "_safe_relogin", return_value=False),
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            budget.return_value.acquire.return_value = None
            r = client.request("GET", "/accounts", headers=client._auth_headers("1"))

        self.assertEqual(r.status_code, 401)
        log_text = engine_log._LOG.read_text(encoding="utf-8")
        self.assertIn("CRITICAL: IG authentication failed — check credentials", log_text)

    def test_token_age_resets_after_successful_refresh(self) -> None:
        client = _client_with_tokens(token_age=IGRestClient.TOKEN_MAX_AGE_SECONDS + 120)
        refresh_resp = requests.Response()
        refresh_resp.status_code = 200
        refresh_resp.headers = {"CST": "new-cst", "X-SECURITY-TOKEN": "new-xst"}
        refresh_resp._content = b'{"accounts":[{"accountId":"ACC1"}]}'

        with patch.object(client._session, "request", return_value=refresh_resp):
            self.assertTrue(client._refresh_session_tokens())

        self.assertLess(client.token_age_seconds(), 5.0)

    def test_auth_failure_does_not_raise_from_request(self) -> None:
        client = _client_with_tokens()
        unauthorized = requests.Response()
        unauthorized.status_code = 401

        with (
            patch("system.rate_limit_manager.get_rate_limit_manager") as mgr,
            patch("system.rest_api_budget.get_rest_api_budget") as budget,
            patch.object(client._session, "request", return_value=unauthorized),
            patch.object(client, "login", side_effect=IGAuthError("bad creds")),
        ):
            mgr.return_value.check_rest_allowed.return_value = None
            budget.return_value.acquire.return_value = None
            try:
                r = client.request("GET", "/positions", headers=client._auth_headers("2"))
            except Exception as exc:
                self.fail(f"request raised unexpectedly: {exc}")

        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
