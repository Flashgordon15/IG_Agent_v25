"""
IG price streaming — REST poll transport when Lightstreamer SDK is unavailable.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ig_api.auth import SessionTokens
from ig_api.exceptions import IGStreamError, RateLimitError
from ig_api.price_subscribers import CallbackList
from system.rate_limit_manager import get_rate_limit_manager
from system.credentials_loader import Credentials
from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.demo_rest_log import log_demo_rest
from system.engine_log import log_engine


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class PriceUpdate:
    epic: str
    bid: float
    offer: float
    timestamp: Any = None


@dataclass
class AccountUpdate:
    balance: float | None = None
    available: float | None = None
    raw: dict[str, Any] | None = None


class IGStreamingClient:
    """Streams MARKET:PRICE via REST polling (credentials + session required)."""

    transport_label = "REST poll"

    def __init__(
        self,
        credentials: Credentials,
        session: SessionTokens,
        *,
        rest_client: Any,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._credentials = credentials
        self._session = session
        self._rest = rest_client
        self._poll_interval = poll_interval_seconds
        self._state = ConnectionState.DISCONNECTED
        self._price_subs = CallbackList[PriceUpdate]()
        self._on_account: Callable[[AccountUpdate], None] | None = None
        self._state_subs = CallbackList[ConnectionState]()
        self._epics: set[str] = set()
        self._thread: threading.Thread | None = None
        self._running = False
        self._failures = 0
        self._max_backoff = 30.0
        self._heartbeat_interval = 120.0
        self._first_tick_received = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_price(self, callback: Callable[[PriceUpdate], None]) -> None:
        self._price_subs.subscribe(callback)

    def on_account(self, callback: Callable[[AccountUpdate], None]) -> None:
        self._on_account = callback

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        self._state_subs.subscribe(callback)

    def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        self._state_subs.emit(state)

    def _mark_connected_on_first_tick(self) -> None:
        if self._first_tick_received:
            return
        self._first_tick_received = True
        self._set_state(ConnectionState.CONNECTED)
        update_demo_diagnostics(streaming_status="connected", streaming_auth_status="authenticated")
        log_demo_rest(
            "IG streaming connected (REST poll)",
            account_type=self._credentials.account_type,
            epics=list(self._epics),
        )
        trace_execution(
            "STREAM",
            "IGStreamingClient._poll_loop",
            decision="streaming authentication success (REST poll transport)",
            next_fn="IGStreamingClient._poll_loop",
        )
        log_engine("Stream CONNECTED — first tick received")

    def connect(self) -> None:
        if not self._session.is_valid:
            raise IGStreamError("Invalid session — login via REST first")
        if self._running:
            return
        self._running = True
        self._failures = 0
        self._first_tick_received = False
        self._set_state(ConnectionState.CONNECTING)
        trace_execution(
            "STREAM",
            "IGStreamingClient.connect",
            decision="connecting",
            params={
                "account_type": self._credentials.account_type,
                "epics": list(self._epics),
            },
        )
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="IGStreamPoll")
        self._thread.start()
        trace_execution(
            "STREAM",
            "IGStreamingClient.connect",
            decision="poll thread started — awaiting first tick",
            next_fn="IGStreamingClient._poll_loop",
        )
        log_engine("IG stream poll thread started")

    def disconnect(self) -> None:
        self._running = False
        self._first_tick_received = False
        self._set_state(ConnectionState.DISCONNECTED)

    def subscribe_market(self, epic: str) -> None:
        self._epics.add(epic)
        trace_execution(
            "STREAM",
            "IGStreamingClient.subscribe_market",
            decision=f"subscribed {epic}",
            params={"epics": list(self._epics)},
        )

    def unsubscribe_market(self, epic: str) -> None:
        self._epics.discard(epic)

    def subscribe_account_summary(self) -> None:
        pass

    def enable_auto_reconnect(self, *, max_attempts: int = 0, base_delay_seconds: float = 2.0) -> None:
        self._max_backoff = max(base_delay_seconds * 8, 30.0)

    def _resubscribe(self) -> None:
        """Re-apply epic subscriptions after reconnect (epics set is retained)."""
        if self._epics:
            log_engine(f"IG stream re-subscribed {len(self._epics)} epic(s)")

    def _poll_loop(self) -> None:
        tick = 0
        last_heartbeat = time.time()
        mgr = get_rate_limit_manager()
        while self._running:
            if not self._epics:
                time.sleep(self._poll_interval)
                continue

            if mgr.is_rest_blocked():
                wait = mgr.seconds_until_rest_reset()
                self._set_state(ConnectionState.RECONNECTING)
                update_demo_diagnostics(
                    streaming_status=f"rate limit — REST paused {int(wait // 60)}m",
                    streaming_auth_status="rate limited (REST)",
                )
                time.sleep(min(max(wait, 5.0), 60.0))
                continue

            if mgr.is_stream_blocked():
                wait = mgr.seconds_until_stream_reset()
                self._set_state(ConnectionState.RECONNECTING)
                update_demo_diagnostics(
                    streaming_status=f"rate limit — stream retry in {int(wait)}s",
                    streaming_auth_status="rate limited",
                )
                time.sleep(min(max(wait, 1.0), 30.0))
                continue

            try:
                from system.market_data_hub import get_market_data_hub

                hub = get_market_data_hub()
                hub.attach_rest(self._rest)
                hub.set_min_fetch_interval(self._poll_interval)
                for epic in list(self._epics):
                    snap = hub.fetch_if_stale(epic, min_interval=self._poll_interval)
                    if not snap or snap.bid <= 0:
                        continue
                    bid, offer = snap.bid, snap.offer
                    pu = PriceUpdate(
                        epic=epic,
                        bid=bid,
                        offer=offer,
                        timestamp=time.time(),
                    )
                    if tick == 0 or tick % 30 == 0:
                        trace_execution(
                            "STREAM",
                            "IGStreamingClient._poll_loop",
                            decision="tick received",
                            params={"epic": epic, "bid": pu.bid, "offer": pu.offer},
                        )
                    self._price_subs.emit(pu)
                    self._mark_connected_on_first_tick()
                self._failures = 0
                tick += 1
                now = time.time()
                if self._on_account and now - last_heartbeat >= self._heartbeat_interval:
                    try:
                        summary = (
                            self._rest.maybe_refresh_account_summary(min_interval=60.0)
                            if hasattr(self._rest, "maybe_refresh_account_summary")
                            else (
                                self._rest.refresh_account_summary()
                                if hasattr(self._rest, "refresh_account_summary")
                                else {}
                            )
                        )
                        bal = summary.get("balance") or self._rest.fetch_account_balance()
                        self._on_account(AccountUpdate(balance=bal, available=bal))
                        trace_execution(
                            "STREAM",
                            "IGStreamingClient._poll_loop",
                            decision="heartbeat",
                            params={"balance": bal},
                        )
                    except Exception:
                        pass
                    last_heartbeat = now
                time.sleep(self._poll_interval)
            except RateLimitError as e:
                self._failures += 1
                log_engine(f"stream rate limited: {e}")
                self._set_state(ConnectionState.RECONNECTING)
                update_demo_diagnostics(
                    streaming_status="rate limited",
                    streaming_auth_status=str(e),
                )
                wait = mgr.seconds_until_stream_reset()
                time.sleep(min(max(wait, 5.0), 60.0))
            except Exception as e:
                self._failures += 1
                log_engine(f"stream poll error #{self._failures}: {type(e).__name__}")
                update_demo_diagnostics(
                    streaming_status=f"error: {type(e).__name__}",
                    streaming_auth_status=f"error: {e}",
                )
                log_demo_rest("stream poll error", failure=self._failures, error=str(e))
                trace_execution(
                    "STREAM",
                    "IGStreamingClient._poll_loop",
                    decision=f"disconnect/error #{self._failures}",
                    params={"error": str(e)},
                )
                if self._failures == 1:
                    self._set_state(ConnectionState.RECONNECTING)
                if mgr.is_rest_blocked() or mgr.is_stream_blocked():
                    wait = max(mgr.seconds_until_rest_reset(), mgr.seconds_until_stream_reset())
                    time.sleep(min(max(wait, 5.0), 60.0))
                    continue
                backoff = min(self._max_backoff, 2.0 ** min(self._failures, 4))
                time.sleep(backoff)
                try:
                    mgr.check_rest_allowed()
                    self._rest.ensure_session()
                    self._resubscribe()
                    self._first_tick_received = False
                    self._set_state(ConnectionState.CONNECTING)
                except RateLimitError:
                    pass
                except Exception:
                    pass
