"""IG account settlement currency for P&L display (not instrument order currency)."""

from __future__ import annotations

from typing import Any

ACCOUNT_CURRENCY = "GBP"

_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def set_account_currency_from_session(session_body: dict[str, Any] | None) -> str:
    """Capture currencyIsoCode from IG POST /session response."""
    global ACCOUNT_CURRENCY
    raw = session_body or {}
    code = str(raw.get("currencyIsoCode") or "GBP").upper()
    ACCOUNT_CURRENCY = code
    return code


def get_account_currency() -> str:
    return ACCOUNT_CURRENCY


def account_currency_symbol() -> str:
    return _SYMBOLS.get(ACCOUNT_CURRENCY, ACCOUNT_CURRENCY + " ")
