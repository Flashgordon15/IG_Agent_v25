"""
IG session authentication state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SessionTokens:
    cst: str
    security_token: str
    account_id: str
    lightstreamer_endpoint: str | None = None
    logged_in_at: datetime = field(default_factory=datetime.utcnow)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return bool(self.cst and self.security_token)

    @property
    def account_currency(self) -> str:
        """Account settlement currency from IG login (currencyIsoCode)."""
        return str(self.raw.get("currencyIsoCode") or "GBP").upper()


def _header_get(headers: dict[str, str], *names: str) -> str | None:
    """Case-insensitive header lookup."""
    lower = {str(k).lower(): v for k, v in headers.items()}
    for name in names:
        val = lower.get(name.lower())
        if val:
            return str(val)
    return None


class AuthManager:
    """Manages login headers and token lifecycle for IGRestClient."""

    def __init__(self, api_key: str, username: str, password: str) -> None:
        self.api_key = api_key
        self.username = username
        self.password = password
        self._tokens: SessionTokens | None = None

    @property
    def tokens(self) -> SessionTokens | None:
        return self._tokens

    def login_headers(self) -> dict[str, str]:
        """Headers for POST /session — no session tokens yet."""
        return {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "VERSION": "2",
        }

    def login_body(self, account_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "identifier": self.username,
            "password": self.password,
        }
        if account_id:
            body["accountId"] = account_id
        return body

    def authenticated_headers(
        self,
        version: str = "3",
        account_id: str | None = None,
    ) -> dict[str, str]:
        if not self._tokens or not self._tokens.is_valid:
            raise RuntimeError("Not authenticated — call login() first")
        acct = account_id or self._tokens.account_id
        headers = {
            "X-IG-API-KEY": self.api_key,
            "CST": self._tokens.cst,
            "X-SECURITY-TOKEN": self._tokens.security_token,
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json; charset=UTF-8",
            "VERSION": str(version),
        }
        if acct:
            headers["IG-ACCOUNT-ID"] = acct
            headers["X-IG-ACCOUNT-ID"] = acct
        return headers

    def apply_login_response(
        self,
        response_headers: dict[str, str],
        response_body: dict[str, Any],
        *,
        preferred_account_id: str | None = None,
    ) -> SessionTokens:
        cst = _header_get(response_headers, "CST", "cst")
        xst = _header_get(
            response_headers,
            "X-SECURITY-TOKEN",
            "x-security-token",
            "IG-SECURITY-TOKEN",
            "ig-security-token",
        )
        if not cst or not xst:
            raise RuntimeError("Login succeeded but CST / X-SECURITY-TOKEN missing from response headers")

        accounts = response_body.get("accounts") or []
        account_id = ""
        if preferred_account_id:
            for acc in accounts:
                if str(acc.get("accountId", "")) == preferred_account_id:
                    account_id = preferred_account_id
                    break
        if not account_id and accounts:
            account_id = str(accounts[0].get("accountId", ""))
        if preferred_account_id and not account_id:
            account_id = preferred_account_id

        ls_endpoint = response_body.get("lightstreamerEndpoint")

        self._tokens = SessionTokens(
            cst=str(cst),
            security_token=str(xst),
            account_id=account_id,
            lightstreamer_endpoint=str(ls_endpoint) if ls_endpoint else None,
            raw=response_body,
        )
        return self._tokens

    def clear(self) -> None:
        self._tokens = None
