"""
BrokerAdapter protocol — separates strategy logic from broker implementation.

Implementing this protocol allows the same strategy code to run against:
  - IGBrokerAdapter  → real IG REST API (DEMO or LIVE)
  - SimBrokerAdapter → in-process simulator (TEST mode / backtesting)

Usage in ExecutionEngine / LiveExecutor is transparent: both adapters expose
the same interface so the strategy never calls broker-specific APIs directly.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrokerAdapter(Protocol):
    """Minimal interface required by the execution engine to interact with a broker."""

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float,
        limit_distance: float,
        currency_code: str,
    ) -> dict[str, Any]:
        """Submit a market order.  Returns a dict containing at minimum
        ``dealReference`` (str).  Raises ``IGAPIError`` / ``IGOrderError`` on
        hard failures; may raise ``RateLimitError`` when throttled."""
        ...

    def confirm_deal(self, deal_reference: str) -> dict[str, Any]:
        """Poll /confirms/{deal_reference} until terminal status.  Returns a
        dict with keys ``accepted`` (bool), ``rejected`` (bool),
        ``deal_id`` (str | None), ``reason`` (str), ``status`` (str)."""
        ...

    # ------------------------------------------------------------------
    # Account / position information
    # ------------------------------------------------------------------

    def fetch_account_balance(self) -> float:
        """Return available cash balance."""
        ...

    def fetch_open_positions(self, epic: str | None = None) -> list[dict[str, Any]]:
        """Return a list of open position dicts, each containing at minimum
        ``dealId``, ``epic``, ``direction``, ``size``, ``level``."""
        ...

    def fetch_price_history(
        self,
        epic: str,
        *,
        resolution: str = "MINUTE_5",
        num_points: int = 288,
    ) -> list[dict[str, Any]]:
        """Return historical OHLCV bars as a list of dicts with keys:
        ``time``, ``open``, ``high``, ``low``, ``close``,
        ``bid_close``, ``offer_close``."""
        ...

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @property
    def account_type(self) -> str:
        """Return 'DEMO' or 'LIVE'."""
        ...

    @property
    def account_id(self) -> str:
        """Return the broker account identifier."""
        ...


class SimBrokerAdapter:
    """Simulator implementation of BrokerAdapter — used in TEST mode and backtesting.

    Executes fills locally via TestSimulator without any IG API calls.
    Swap this in place of IGBrokerAdapter to run the full strategy pipeline
    against historical data or unit tests without any network dependency.
    """

    def __init__(
        self,
        *,
        account_type: str = "DEMO",
        account_id: str = "SIM",
        fill_price_fn: Any | None = None,
    ) -> None:
        self._account_type = account_type
        self._account_id = account_id
        self._fill_price_fn = fill_price_fn
        self._open_positions: list[dict[str, Any]] = []
        self._deal_counter = 0

    @property
    def account_type(self) -> str:
        return self._account_type

    @property
    def account_id(self) -> str:
        return self._account_id

    def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float,
        limit_distance: float,
        currency_code: str,
    ) -> dict[str, Any]:
        self._deal_counter += 1
        ref = f"SIM-{self._deal_counter:05d}"
        return {"dealReference": ref, "status": "PENDING"}

    def confirm_deal(self, deal_reference: str) -> dict[str, Any]:
        deal_id = f"SIMID-{deal_reference}"
        self._open_positions.append({"dealId": deal_id, "dealReference": deal_reference})
        return {
            "accepted": True,
            "rejected": False,
            "deal_id": deal_id,
            "deal_reference": deal_reference,
            "reason": "",
            "status": "ACCEPTED",
        }

    def fetch_account_balance(self) -> float:
        return 10_000.0

    def fetch_open_positions(self, epic: str | None = None) -> list[dict[str, Any]]:
        if epic:
            return [p for p in self._open_positions if p.get("epic") == epic]
        return list(self._open_positions)

    def fetch_price_history(
        self,
        epic: str,
        *,
        resolution: str = "MINUTE_5",
        num_points: int = 288,
    ) -> list[dict[str, Any]]:
        return []


def wrap_rest_client(rest_client: Any) -> BrokerAdapter:
    """Return *rest_client* cast as a BrokerAdapter — the real IG REST client
    already satisfies the protocol; this is a thin typing helper."""
    assert isinstance(rest_client, BrokerAdapter), (
        f"rest_client {type(rest_client).__name__} does not satisfy BrokerAdapter protocol"
    )
    return rest_client  # type: ignore[return-value]
