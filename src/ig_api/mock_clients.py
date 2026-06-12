"""
In-process IG API mocks for TEST mode and LIVE mode without credentials.

Used when REST/streaming production clients are unavailable.
"""

from __future__ import annotations

import random
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ig_api.auth import SessionTokens
from ig_api.exceptions import IGAuthError, IGOrderError
from ig_api.price_subscribers import CallbackList
from ig_api.streaming_client import AccountUpdate, ConnectionState, PriceUpdate


@dataclass
class MockRESTConfig:
    latency_ms: float = 0
    reject_order: bool = False
    balance: float = 100_000.0


class MockIGRest:
    """Mock REST — same interface as :class:`~ig_api.rest_client.IGRestClient`."""

    def __init__(
        self,
        *,
        api_key: str = "mock-key",
        username: str = "mock-user",
        password: str = "mock-pass",
        account_type: str = "DEMO",
        account_id: str = "MOCK-ACCOUNT",
        mock_config: MockRESTConfig | None = None,
        initial_bid: float = 30000.0,
        initial_offer: float = 30002.0,
    ) -> None:
        self.api_key = api_key
        self.username = username
        self.password = password
        self.account_type = account_type.upper()
        self.account_id = account_id
        self.mock = mock_config or MockRESTConfig()
        self._bid = initial_bid
        self._offer = initial_offer
        self._session = SessionTokens(
            cst="MOCK-CST",
            security_token="MOCK-XST",
            account_id=account_id,
            lightstreamer_endpoint="https://mock.lightstreamer.test",
        )
        self._positions: list[dict[str, Any]] = []
        self._logged_in = False

    @property
    def session(self) -> SessionTokens:
        return self._session

    def set_quote(self, bid: float, offer: float) -> None:
        self._bid = bid
        self._offer = offer

    def login(self) -> SessionTokens:
        self._logged_in = True
        return self._session

    def refresh_session(self) -> SessionTokens:
        return self._session

    def ensure_session(self) -> None:
        if not self._logged_in:
            self.login()

    def fetch_market_snapshot(self, epic: str) -> dict[str, Any]:
        return {"epic": epic, "bid": self._bid, "offer": self._offer}

    def fetch_account_balance(self) -> float:
        return self.mock.balance

    def open_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    def has_open_position(self, epic: str) -> bool:
        return any(p.get("epic") == epic for p in self._positions)

    def place_market_order(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float,
        limit_distance: float,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        if self.mock.reject_order:
            raise IGOrderError("Mock order rejected", status_code=400)
        ref = "MOCK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        deal_id = "D" + ref[5:]
        self._positions.append(
            {
                "epic": epic,
                "direction": direction,
                "size": size,
                "dealId": deal_id,
                "stop_distance": float(stop_distance),
                "limit_distance": float(limit_distance),
            }
        )
        self._last_deal_id = deal_id
        self._last_deal_reference = ref
        return {"dealReference": ref}

    def place_limit_entry_atomic(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        level: float,
        stop_distance: float,
        limit_distance: float | None = None,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        return self.place_market_order(
            epic=epic,
            direction=direction,
            size=size,
            stop_distance=stop_distance,
            limit_distance=float(limit_distance or 0),
            currency_code=currency_code,
        )

    def find_open_position(self, deal_id: str) -> dict[str, Any] | None:
        for p in self._positions:
            if str(p.get("dealId")) == str(deal_id):
                return {
                    "market": {"epic": p.get("epic")},
                    "position": {
                        "dealId": deal_id,
                        "direction": p.get("direction"),
                        "size": p.get("size"),
                        "stopDistance": p.get("stop_distance", 10),
                        "limitDistance": p.get("limit_distance", 30),
                    },
                }
        return None

    def position_protection_status(self, deal_id: str) -> bool:
        row = self.find_open_position(deal_id)
        if not row:
            return False
        pos = row.get("position") or {}
        return float(pos.get("stopDistance") or 0) > 0 and float(
            pos.get("limitDistance") or 0
        ) > 0

    def cancel_all_working_orders(self, epic: str | None = None) -> int:
        return 0

    def flatten_all_positions(self) -> int:
        n = len(self._positions)
        self._positions.clear()
        return n

    def confirm_deal(self, deal_reference: str, **kwargs: Any) -> dict[str, Any]:
        deal_id = getattr(self, "_last_deal_id", None)
        if str(deal_reference) != str(getattr(self, "_last_deal_reference", "")):
            deal_id = None
        if not deal_id:
            deal_id = "D" + str(deal_reference)[5:]
        return {
            "terminal": True,
            "accepted": True,
            "rejected": False,
            "deal_id": deal_id,
            "reason": "",
        }


@dataclass
class MockStreamConfig:
    tick_interval_ms: float = 500
    volatility: float = 1.0


class MockIGStreaming:
    """Mock streaming — compatible with :class:`~ig_api.streaming_client.IGStreamingClient`."""

    def __init__(
        self,
        *,
        lightstreamer_endpoint: str = "https://mock.ls",
        api_key: str = "mock",
        session: SessionTokens | None = None,
        account_id: str = "MOCK-ACCOUNT",
        config: MockStreamConfig | None = None,
    ) -> None:
        self._session = session or SessionTokens("CST", "XST", account_id)
        self.config = config or MockStreamConfig()
        self._state = ConnectionState.DISCONNECTED
        self._price_subs = CallbackList[PriceUpdate]()
        self._on_account: Callable[[AccountUpdate], None] | None = None
        self._state_subs = CallbackList[ConnectionState]()
        self._epics: set[str] = set()
        self._thread: threading.Thread | None = None
        self._running = False
        self._bid = 30000.0
        self._offer = 30002.0

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_price(self, callback: Callable[[PriceUpdate], None]) -> None:
        self._price_subs.subscribe(callback)

    def on_account(self, callback: Callable[[AccountUpdate], None]) -> None:
        self._on_account = callback

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        self._state_subs.subscribe(callback)

    def set_quote(self, bid: float, offer: float) -> None:
        self._bid = bid
        self._offer = offer

    def _set_state(self, state: ConnectionState) -> None:
        self._state = state
        self._state_subs.emit(state)

    def connect(self) -> None:
        self._set_state(ConnectionState.CONNECTING)
        self._set_state(ConnectionState.CONNECTED)
        self._running = True
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._running = False
        self._set_state(ConnectionState.DISCONNECTED)

    def subscribe_market(self, epic: str) -> None:
        self._epics.add(epic)

    def unsubscribe_market(self, epic: str) -> None:
        self._epics.discard(epic)

    def subscribe_account_summary(self) -> None:
        pass

    def enable_auto_reconnect(self, **kwargs: Any) -> None:
        pass

    def _tick_loop(self) -> None:
        while self._running:
            shock = random.uniform(-self.config.volatility, self.config.volatility)
            self._bid += shock
            self._offer = self._bid + 2.0
            if self._epics:
                for epic in list(self._epics):
                    self._price_subs.emit(
                        PriceUpdate(epic=epic, bid=self._bid, offer=self._offer, timestamp=time.time())
                    )
            time.sleep(self.config.tick_interval_ms / 1000.0)
