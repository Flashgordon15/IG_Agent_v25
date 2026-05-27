"""
IG REST endpoint path builders.

All URLs are relative to the gateway base (demo or live).
"""

from __future__ import annotations

from enum import Enum


class AccountType(str, Enum):
    DEMO = "DEMO"
    LIVE = "LIVE"


def base_url(account_type: AccountType) -> str:
    """Return gateway base URL for demo or live account."""
    raise NotImplementedError


def session() -> str:
    """POST — create session / login."""
    return "/session"


def session_refresh() -> str:
    """POST — refresh session tokens."""
    return "/session/refresh"


def market(epic: str) -> str:
    """GET — market details and snapshot."""
    return f"/markets/{epic}"


def positions() -> str:
    """GET — open positions."""
    return "/positions"


def position_otc() -> str:
    """POST — open OTC position (market order)."""
    return "/positions/otc"


def confirm(deal_reference: str) -> str:
    """GET — poll deal confirmation."""
    return f"/confirms/{deal_reference}"


def accounts() -> str:
    """GET — account list / summary."""
    return "/accounts"


def update_position(deal_id: str) -> str:
    """PUT — update stop/limit on open position."""
    return f"/positions/otc/{deal_id}"
