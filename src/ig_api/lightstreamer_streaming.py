"""
IG Lightstreamer streaming — optional transport with REST poll fallback.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ig_api.auth import SessionTokens
from ig_api.streaming_client import ConnectionState, IGStreamingClient, PriceUpdate
from ig_api.price_subscribers import CallbackList
from system.credentials_loader import Credentials
from system.engine_log import log_engine

_MAX_LS_RECONNECT = 3
_LS_RECONNECT_WAIT_SEC = 5.0
_LS_CONNECTING_GRACE_SEC = 90.0
_LS_BLANK_TICK_RECONNECT_SEC = 30.0
_BLANK_TICK_LOG_INTERVAL_SEC = 30.0


class IGLightstreamerStreamingClient:
    """Lightstreamer MARKET subscription; falls back to REST poll on failure."""

    def __init__(
        self,
        credentials: Credentials,
        session: SessionTokens,
        *,
        rest_client: Any,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self._credentials = credentials
        self._session = session
        self._rest = rest_client
        self._poll_interval = poll_interval_seconds
        self._state = ConnectionState.DISCONNECTED
        self._price_subs = CallbackList[PriceUpdate]()
        self._state_subs = CallbackList[ConnectionState]()
        self._epics: set[str] = set()
        self._running = False
        self._ls_client: Any = None
        self._subscriptions: list[Any] = []
        self._fallback: IGStreamingClient | None = None
        self._using_fallback = False
        self._lock = threading.Lock()
        self._connect_thread: threading.Thread | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._auto_reconnect = False
        self._first_tick_received = False
        self._connect_attempt_ts = 0.0
        self._last_blank_tick_log_ts = 0.0
        self._first_valid_tick_deadline_ts = 0.0
        self._blank_tick_resubscribe_scheduled = False
        self._blank_recovery_thread: threading.Thread | None = None

    @property
    def connect_attempt_ts(self) -> float:
        return self._connect_attempt_ts

    @property
    def connecting_grace_seconds(self) -> float:
        return _LS_CONNECTING_GRACE_SEC

    def _handle_blank_tick(self, epic: str) -> None:
        from system.market_data_hub import get_market_data_hub

        get_market_data_hub().enter_maintenance(epic)
        now = time.time()
        if now - self._last_blank_tick_log_ts >= _BLANK_TICK_LOG_INTERVAL_SEC:
            self._last_blank_tick_log_ts = now
            remaining = max(0.0, self._first_valid_tick_deadline_ts - now)
            log_engine(
                f"LS blank BID/OFFER — maintenance epic={epic} "
                f"(recovery in {remaining:.0f}s if no valid tick)"
            )
        self._maybe_schedule_blank_tick_recovery(epic)

    def _maybe_schedule_blank_tick_recovery(self, epic: str) -> None:
        if self._using_fallback or not self._running or self._first_tick_received:
            return
        deadline = self._first_valid_tick_deadline_ts
        if deadline <= 0 or time.time() < deadline:
            return
        if self._blank_tick_resubscribe_scheduled:
            return
        self._blank_tick_resubscribe_scheduled = True
        log_engine(
            f"LS no valid tick within {_LS_BLANK_TICK_RECONNECT_SEC}s — "
            f"starting blank-tick recovery epic={epic}"
        )
        self._schedule_blank_tick_recovery(epic)

    def _arm_blank_tick_deadline_timer(self, epic: str) -> None:
        wait = _LS_BLANK_TICK_RECONNECT_SEC
        outer = self

        def _fire() -> None:
            if not outer._running or outer._using_fallback:
                return
            outer._maybe_schedule_blank_tick_recovery(epic)

        threading.Timer(wait, _fire).start()

    def _mark_connected_on_first_tick(self, bid: float, offer: float, epic: str) -> None:
        if self._first_tick_received:
            return
        self._first_tick_received = True
        self._first_valid_tick_deadline_ts = 0.0
        self._blank_tick_resubscribe_scheduled = False
        self._set_state(ConnectionState.CONNECTED)
        log_engine(
            f"Lightstreamer CONNECTED — first tick received bid={bid} offer={offer} epic={epic}"
        )
        try:
            from system.stream_ready import signal_stream_ready

            signal_stream_ready(source=f"lightstreamer:{epic}")
        except Exception:
            pass

    @property
    def transport_label(self) -> str:
        if self._using_fallback and self._fallback:
            fb = getattr(self._fallback, "transport_label", "REST poll")
            return fb() if callable(fb) else str(fb)
        return "Lightstreamer"

    @property
    def state(self) -> ConnectionState:
        if self._using_fallback and self._fallback:
            return self._fallback.state
        return self._state

    def on_price(self, callback: Callable[[PriceUpdate], None]) -> None:
        self._price_subs.subscribe(callback)

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        self._state_subs.subscribe(callback)

    def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        self._state_subs.emit(state)

    def subscribe_market(self, epic: str) -> None:
        self._epics.add(epic)
        if self._using_fallback and self._fallback:
            self._fallback.subscribe_market(epic)

    def unsubscribe_market(self, epic: str) -> None:
        self._epics.discard(epic)

    def connect(self) -> None:
        if not self._session.is_valid:
            raise RuntimeError("Invalid session — login via REST first")
        if self._running:
            return
        self._running = True
        self._first_tick_received = False
        self._first_valid_tick_deadline_ts = 0.0
        self._blank_tick_resubscribe_scheduled = False
        self._auto_reconnect = True
        self._set_state(ConnectionState.CONNECTING)
        self._connect_thread = threading.Thread(
            target=self._connect_worker,
            daemon=True,
            name="IGLightstreamerConnect",
        )
        self._connect_thread.start()

    def _connect_worker(self) -> None:
        for attempt in range(1, _MAX_LS_RECONNECT + 1):
            if attempt > 1:
                log_engine(f"Lightstreamer reconnect attempt {attempt} of {_MAX_LS_RECONNECT}")
                time.sleep(_LS_RECONNECT_WAIT_SEC)
            try:
                self._teardown_lightstreamer()
                self._first_tick_received = False
                self._connect_lightstreamer()
                deadline = time.time() + 10.0
                while time.time() < deadline and self._running and not self._using_fallback:
                    if self._first_tick_received:
                        if attempt > 1:
                            log_engine("Lightstreamer reconnected — awaiting first tick")
                        else:
                            log_engine(
                                f"Lightstreamer subscribed (awaiting first tick) epics={list(self._epics)}"
                            )
                        return
                    time.sleep(0.25)
                if self._using_fallback:
                    return
                if attempt > 1:
                    log_engine("Lightstreamer reconnected — awaiting first tick")
                else:
                    log_engine(
                        f"Lightstreamer subscribed (awaiting first tick) epics={list(self._epics)}"
                    )
                return
            except Exception as e:
                log_engine(
                    f"Lightstreamer connect attempt {attempt} failed: {type(e).__name__}: {e}"
                )
        log_engine(
            f"Lightstreamer failed after {_MAX_LS_RECONNECT} attempts — REST poll fallback"
        )
        self._start_fallback()

    def _connect_lightstreamer(self) -> None:
        from lightstreamer.client import LightstreamerClient, Subscription
        from system.rest_api_budget import reset_connecting_market_rescue

        reset_connecting_market_rescue()
        endpoint = self._session.lightstreamer_endpoint
        if not endpoint:
            raise RuntimeError("No lightstreamerEndpoint in session")

        # IG trading-ig sample: LightstreamerClient(endpoint, None) — no adapter set name.
        adapter_set = None
        account_id = self._session.account_id
        if not account_id:
            raise RuntimeError("No account_id in session — required for Lightstreamer auth")
        ls_password = f"CST-{self._session.cst}|XST-{self._session.security_token}"
        log_engine(
            f"LS connect: endpoint={endpoint} adapter=default "
            f"user={account_id} password_format=CST-...|XST-..."
        )
        client = LightstreamerClient(endpoint, adapter_set)
        client.connectionDetails.setUser(account_id)
        client.connectionDetails.setPassword(ls_password)
        self._connect_attempt_ts = time.time()
        outer = self

        class _ClientListener:
            def onListenStart(self, _owner: Any) -> None:
                pass

            def onStatusChange(self, status: str) -> None:
                if outer._using_fallback or not outer._running:
                    return
                log_engine(f"LS client status: {status}")
                status_u = str(status or "").upper()
                if "DISCONNECTED" in status_u and "WILL-RETRY" not in status_u:
                    connect_age = time.time() - outer._connect_attempt_ts
                    if (
                        outer._connect_attempt_ts > 0
                        and connect_age <= 10.0
                        and not outer._first_tick_received
                    ):
                        log_engine(
                            "LS auth/connect failed — activating REST poll fallback"
                        )
                        outer._start_fallback()
                        return
                    outer._set_state(ConnectionState.RECONNECTING)
                    outer._first_tick_received = False
                    if outer._auto_reconnect:
                        outer._schedule_lightstreamer_reconnect()
                elif status_u == "CONNECTED":
                    if not outer._first_tick_received:
                        outer._set_state(ConnectionState.CONNECTING)

        client.addListener(_ClientListener())
        client.connect()
        self._ls_client = client
        self._subscriptions = []

        for epic in list(self._epics):
            item_name = f"MARKET:{epic}"
            fields = ["BID", "OFFER", "UPDATE_TIME"]
            mode = "MERGE"
            log_engine(
                f"LS subscribing: item={item_name} fields={fields} mode={mode} "
                f"adapter_set=default data_adapter=default"
            )
            sub = Subscription(mode, [item_name], fields)
            sub.addListener(self._build_market_listener(epic, item_name))
            client.subscribe(sub)
            self._subscriptions.append(sub)

    @staticmethod
    def _epic_from_item_name(item_name: str, fallback: str) -> str:
        raw = str(item_name or "").strip()
        if raw.startswith("MARKET:"):
            return raw.split(":", 1)[1]
        return fallback

    def _build_market_listener(self, epic: str, item_name: str) -> Any:
        """Build LS listener with epic bound per subscription (avoid loop closure bug)."""
        epic_name = str(epic)
        bound_item = str(item_name)
        price_subs = self._price_subs
        ls_client = self

        class _Listener:
            def onSubscription(self) -> None:
                log_engine(f"LS subscription confirmed: item={bound_item}")
                ls_client._first_valid_tick_deadline_ts = (
                    time.time() + _LS_BLANK_TICK_RECONNECT_SEC
                )
                ls_client._arm_blank_tick_deadline_timer(epic_name)

            def onUnsubscription(self) -> None:
                log_engine(f"LS unsubscribed: item={bound_item}")

            def onSubscriptionError(self, code: int, message: str) -> None:
                log_engine(
                    f"LS subscription ERROR: item={bound_item} code={code} message={message}"
                )

            def onItemUpdate(self, update: Any) -> None:
                try:
                    item = (
                        update.getItemName()
                        if hasattr(update, "getItemName")
                        else bound_item
                    )
                    resolved_epic = IGLightstreamerStreamingClient._epic_from_item_name(
                        str(item), epic_name
                    )
                    field_values = {}
                    if hasattr(update, "getFields"):
                        field_values = dict(update.getFields())
                    raw_bid = update.getValue("BID") if hasattr(update, "getValue") else None
                    raw_offer = update.getValue("OFFER") if hasattr(update, "getValue") else None
                    bid_text = str(raw_bid or "").strip()
                    offer_text = str(raw_offer or "").strip()
                    if not bid_text or not offer_text:
                        ls_client._handle_blank_tick(resolved_epic)
                        return
                    from system.engine_log import log_engine_intermittent

                    log_engine_intermittent(
                        f"ls_tick:{resolved_epic}",
                        f"LS raw tick received: item={item} values={field_values}",
                    )
                    bid = float(bid_text)
                    offer = float(offer_text)
                except (TypeError, ValueError):
                    return
                if bid <= 0 or offer <= 0:
                    ls_client._handle_blank_tick(resolved_epic)
                    return
                from system.market_data_hub import get_market_data_hub

                get_market_data_hub().publish(
                    resolved_epic, bid, offer, source="lightstreamer"
                )
                log_engine_intermittent(
                    f"hub_quote:{resolved_epic}",
                    f"Hub quote updated from Lightstreamer: bid={bid} offer={offer} "
                    f"age=0s epic={resolved_epic}",
                )
                ls_client._mark_connected_on_first_tick(bid, offer, resolved_epic)
                pu = PriceUpdate(
                    epic=resolved_epic, bid=bid, offer=offer, timestamp=time.time()
                )
                price_subs.emit(pu)

        return _Listener()

    def _schedule_blank_tick_recovery(self, epic: str) -> None:
        if self._using_fallback or not self._running:
            return
        t = self._blank_recovery_thread
        if t is not None and t.is_alive():
            return

        def work() -> None:
            try:
                self._connect_attempt_ts = time.time()
                self._first_tick_received = False
                self._blank_tick_resubscribe_scheduled = False
                self._teardown_lightstreamer()
                self._connect_lightstreamer()
                log_engine(f"LS blank-tick recovery complete — awaiting first tick epic={epic}")
            except Exception as e:
                log_engine(
                    f"LS blank-tick recovery failed: {type(e).__name__}: {e}"
                )
                if self._auto_reconnect:
                    self._schedule_lightstreamer_reconnect()

        self._blank_recovery_thread = threading.Thread(
            target=work, daemon=True, name="IGLightstreamerBlankRecovery"
        )
        self._blank_recovery_thread.start()

    def _schedule_lightstreamer_reconnect(self) -> None:
        if self._using_fallback or not self._running:
            return
        t = self._reconnect_thread
        if t is not None and t.is_alive():
            return

        def work() -> None:
            for attempt in range(1, _MAX_LS_RECONNECT + 1):
                if not self._running or self._using_fallback:
                    return
                log_engine(f"Lightstreamer reconnect attempt {attempt} of {_MAX_LS_RECONNECT}")
                time.sleep(_LS_RECONNECT_WAIT_SEC)
                try:
                    self._teardown_lightstreamer()
                    self._first_tick_received = False
                    self._connect_lightstreamer()
                    log_engine("Lightstreamer reconnected — awaiting first tick")
                    return
                except Exception as e:
                    log_engine(
                        f"Lightstreamer reconnect attempt {attempt} failed: "
                        f"{type(e).__name__}: {e}"
                    )
            log_engine(
                f"Lightstreamer failed after {_MAX_LS_RECONNECT} attempts — REST poll fallback"
            )
            self._start_fallback()

        self._reconnect_thread = threading.Thread(
            target=work, daemon=True, name="IGLightstreamerReconnect"
        )
        self._reconnect_thread.start()

    def _teardown_lightstreamer(self) -> None:
        if self._ls_client:
            try:
                for sub in list(self._subscriptions):
                    self._ls_client.unsubscribe(sub)
                self._ls_client.disconnect()
            except Exception:
                pass
        self._ls_client = None
        self._subscriptions = []
        self._first_tick_received = False
        self._first_valid_tick_deadline_ts = 0.0

    def _start_fallback(self) -> None:
        if self._using_fallback:
            return
        self._using_fallback = True
        try:
            from system.telegram_notifier import get_telegram_notifier

            notifier = get_telegram_notifier()
            if notifier is not None:
                notifier.notify_rest_fallback()
        except Exception:
            pass
        self._teardown_lightstreamer()
        self._fallback = IGStreamingClient(
            self._credentials,
            self._session,
            rest_client=self._rest,
            poll_interval_seconds=self._poll_interval,
        )
        for epic in self._epics:
            self._fallback.subscribe_market(epic)
        for cb in list(self._price_subs._callbacks):  # noqa: SLF001 — shared subscriber list
            self._fallback.on_price(cb)
        for cb in list(self._state_subs._callbacks):  # noqa: SLF001
            self._fallback.on_state_change(cb)
        self._fallback.connect()

    def disconnect(self) -> None:
        self._running = False
        self._first_tick_received = False
        self._first_valid_tick_deadline_ts = 0.0
        self._blank_tick_resubscribe_scheduled = False
        if self._fallback:
            try:
                self._fallback.disconnect()
            except Exception:
                pass
        self._teardown_lightstreamer()
        self._set_state(ConnectionState.DISCONNECTED)

    def subscribe_account_summary(self) -> None:
        pass

    def enable_auto_reconnect(self, *, max_attempts: int = 0, base_delay_seconds: float = 2.0) -> None:
        self._auto_reconnect = True
        if self._fallback:
            self._fallback.enable_auto_reconnect(
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
            )
