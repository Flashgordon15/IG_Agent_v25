"""
IG price streaming — REST poll transport when Lightstreamer SDK is unavailable.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ig_api.auth import SessionTokens
from ig_api.exceptions import IGStreamError, RateLimitError
from ig_api.rest_poll_backoff import (
    RestPollBackoff,
    format_backoff_warning,
    is_retryable_poll_error,
    soft_streaming_status,
)
from ig_api.price_subscribers import CallbackList
from system.rate_limit_manager import get_rate_limit_manager
from system.credentials_loader import Credentials
from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.demo_rest_log import log_demo_rest
from system.engine_log import log_engine

_PING_INTERVAL_SEC = 3.0
_PING_TIMEOUT_SEC = 2.0
_network_stable = True
_network_uptime_ok = 0
_network_lock = threading.Lock()
_sentinel_stop = threading.Event()
_sentinel_thread: threading.Thread | None = None
_rehandshake_lock = threading.Lock()
_execution_halt_last_log_mono = 0.0
_EXECUTION_HALT_LOG_INTERVAL_SEC = 30.0


class _NetworkLogFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = datetime.fromtimestamp(record.created)
        return f"{ct.strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d}"


def _network_log_path() -> Any:
    from pathlib import Path

    from system.paths import project_root

    log_dir = project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "network_stability.log"


def _network_stability_logger() -> logging.Logger:
    logger = logging.getLogger("ig_agent.network_stability")
    if getattr(logger, "_ig_agent_file_configured", False):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(_network_log_path(), encoding="utf-8")
    handler.setFormatter(
        _NetworkLogFormatter("[%(asctime)s] %(message)s")
    )
    logger.addHandler(handler)
    logger._ig_agent_file_configured = True  # type: ignore[attr-defined]
    return logger


def log_network_stability(message: str) -> None:
    _network_stability_logger().info(message)


def get_network_stable() -> bool:
    with _network_lock:
        return _network_stable


def _set_network_stable(stable: bool) -> None:
    global _network_stable
    with _network_lock:
        _network_stable = stable


def log_execution_halt() -> None:
    global _execution_halt_last_log_mono
    now = time.monotonic()
    if now - _execution_halt_last_log_mono < _EXECUTION_HALT_LOG_INTERVAL_SEC:
        return
    _execution_halt_last_log_mono = now
    log_network_stability(
        "[EXECUTION HALT] Core loop paused. Preventing trade initialization "
        "during network blackout state."
    )


def _ig_ping_host() -> str:
    try:
        from system.credentials_holder import get_credentials_holder

        creds = get_credentials_holder().credentials
        if creds is not None and str(getattr(creds, "account_type", "") or "").upper() == "LIVE":
            return "api.ig.com"
    except Exception:
        pass
    return "demo-api.ig.com"


def _ping_tcp(host: str, port: int = 443, *, timeout: float = _PING_TIMEOUT_SEC) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ping_target(target: str) -> tuple[bool, str]:
    if target == "cloudflare":
        ok = _ping_tcp("1.1.1.1")
        return ok, "1.1.1.1"
    host = _ig_ping_host()
    ok = _ping_tcp(host)
    return ok, host


def _perform_clean_stream_rehandshake() -> None:
    if not _rehandshake_lock.acquire(blocking=False):
        return
    try:
        from runtime.agent_bootstrap import start_market_stream, stop_market_stream
        from system.config_loader import get_config
        from system.credentials_holder import get_credentials_holder
        from system.ig_rest_session import ensure_shared_authenticated

        stop_market_stream()
        creds = get_credentials_holder().credentials
        if creds is None:
            log_engine("network re-handshake skipped — no credentials")
            return
        rest = ensure_shared_authenticated(creds)
        if hasattr(rest, "ensure_session"):
            rest.ensure_session()
        cfg = get_config()
        start_market_stream(cfg, rest_client=rest, clear_stream_ready=True)
        pid = os.getpid()
        log_network_stability(
            f"[RECONFIG COMPLETE] Stream tickers re-subscribed. PID active and tracking."
        )
        log_engine(f"network re-handshake complete pid={pid}")
    except Exception as e:
        log_engine(f"network re-handshake failed: {type(e).__name__}: {e}")
        log_network_stability(
            f"[RECONFIG FAILED] Stream re-handshake error: {type(e).__name__}: {e}"
        )
    finally:
        _rehandshake_lock.release()


def network_heartbeat_sentinel() -> None:
    """Background loop — alternate Cloudflare + IG TCP reachability every 3s."""
    global _network_uptime_ok
    targets = ("cloudflare", "ig_api")
    idx = 0
    while not _sentinel_stop.is_set():
        target = targets[idx % len(targets)]
        idx += 1
        ok, host = _ping_target(target)
        if ok:
            _network_uptime_ok += 1
            if not get_network_stable():
                _set_network_stable(True)
                log_network_stability(
                    "[CONNECTION RESTORED] Internet link recovered. "
                    "Initializing clean socket re-handshake."
                )
                log_engine(f"network restored via ping host={host}")
                _perform_clean_stream_rehandshake()
        else:
            if get_network_stable():
                _set_network_stable(False)
                log_network_stability(
                    "[DISCONNECT DETECTED] Network ping failed. "
                    "Status: ConnectionError."
                )
                log_engine(f"network disconnect detected ping host={host}")
        _sentinel_stop.wait(_PING_INTERVAL_SEC)


def ensure_network_heartbeat_sentinel_started() -> None:
    global _sentinel_thread
    if _sentinel_thread is not None and _sentinel_thread.is_alive():
        return
    _sentinel_stop.clear()
    _sentinel_thread = threading.Thread(
        target=network_heartbeat_sentinel,
        name="network-heartbeat-sentinel",
        daemon=True,
    )
    _sentinel_thread.start()
    log_network_stability(
        "[SENTINEL START] Network heartbeat monitor active "
        f"(interval={_PING_INTERVAL_SEC:.0f}s, targets=1.1.1.1/IG)."
    )
    log_engine("network heartbeat sentinel started (3s Cloudflare/IG alternation)")


def stop_network_heartbeat_sentinel() -> None:
    global _sentinel_thread
    _sentinel_stop.set()
    t = _sentinel_thread
    if t is not None and t.is_alive():
        t.join(timeout=5.0)
    _sentinel_thread = None


def reset_network_stability_for_tests() -> None:
    global _network_uptime_ok, _execution_halt_last_log_mono
    stop_network_heartbeat_sentinel()
    _set_network_stable(True)
    _network_uptime_ok = 0
    _execution_halt_last_log_mono = 0.0


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
        self._poll_backoff = RestPollBackoff(poll_interval_seconds)

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
        try:
            from system.stream_ready import signal_stream_ready

            signal_stream_ready(source="rest_poll")
        except Exception:
            pass
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
        # A disconnect() flips _running but the poll thread may still be inside
        # its sleep. Re-connecting then would revive that old thread AND spawn a
        # second one — reuse the surviving thread instead of stacking a new one.
        if self._thread is not None and self._thread.is_alive():
            self._running = True
            self._set_state(ConnectionState.CONNECTING)
            log_engine("IG stream poll thread re-armed (existing thread reused)")
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
        ensure_network_heartbeat_sentinel_started()
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
        backoff = self._poll_backoff
        while self._running:
            if not self._epics:
                time.sleep(backoff.normal_interval)
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
                last_heartbeat = self._poll_once(tick, last_heartbeat)
                tick += 1
                sleep_s = backoff.on_success()
                if self._first_tick_received:
                    update_demo_diagnostics(
                        streaming_status="connected",
                        streaming_auth_status="authenticated",
                    )
                time.sleep(sleep_s)
            except RateLimitError as e:
                self._handle_retryable_poll_error(e, backoff, mgr)
            except Exception as e:
                if is_retryable_poll_error(e):
                    self._handle_retryable_poll_error(e, backoff, mgr)
                    continue
                self._handle_poll_error(e, backoff, mgr)

    def _poll_once(self, tick: int, last_heartbeat: float) -> float:
        """One REST poll cycle across subscribed epics. Updates last_heartbeat in-place."""
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        hub.attach_rest(self._rest)
        hub.set_min_fetch_interval(self._poll_interval)
        backoff = self._poll_backoff
        backoff.set_normal_interval(self._poll_interval)

        got_tick = False
        from system.rest_api_budget import stream_quote_poll_rest_window

        with stream_quote_poll_rest_window():
            epics_this_tick = list(self._epics)
            for epic in epics_this_tick:
                snap = hub.fetch_if_stale(
                    epic,
                    min_interval=0.0,
                    propagate_transient_errors=True,
                )
                if not snap or snap.bid <= 0:
                    continue
                got_tick = True
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

        if got_tick:
            from system.rest_poll_status import record_poll_success

            record_poll_success()
            self._failures = 0
        else:
            from system.rest_poll_status import record_poll_cycle_without_tick

            record_poll_cycle_without_tick()

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
            return now
        return last_heartbeat

    def _handle_retryable_poll_error(
        self,
        exc: BaseException,
        backoff: RestPollBackoff,
        mgr: Any,
    ) -> None:
        """429 / timeout — short soak-safe back-off, no panic or session teardown."""
        wait_s, label = backoff.on_retryable_error(exc)
        log_engine(format_backoff_warning(label, wait_s, strike=backoff.strike))
        update_demo_diagnostics(
            streaming_status=soft_streaming_status(label, wait_s),
            streaming_auth_status="backoff (transient)",
        )
        if not self._first_tick_received:
            self._set_state(ConnectionState.RECONNECTING)
        time.sleep(wait_s)

    def _handle_poll_error(
        self,
        exc: BaseException,
        backoff: RestPollBackoff,
        mgr: Any,
    ) -> None:
        """Non-transient errors — gentle recovery without health-monitor panic."""
        self._failures += 1
        log_engine(
            f"rest_poll warning #{self._failures}: {type(exc).__name__}: {exc}"
        )
        update_demo_diagnostics(
            streaming_status=f"rest_poll recover #{self._failures}",
            streaming_auth_status=f"{type(exc).__name__}",
        )
        if self._failures == 1 and not self._first_tick_received:
            self._set_state(ConnectionState.RECONNECTING)
        if mgr.is_rest_blocked() or mgr.is_stream_blocked():
            wait = max(mgr.seconds_until_rest_reset(), mgr.seconds_until_stream_reset())
            time.sleep(min(max(wait, 5.0), 60.0))
            return
        sleep_s = min(self._max_backoff, 2.0 ** min(self._failures, 4))
        time.sleep(sleep_s)
        try:
            mgr.check_rest_allowed()
            self._rest.ensure_session()
            self._resubscribe()
            if not self._first_tick_received:
                self._set_state(ConnectionState.CONNECTING)
        except RateLimitError as rate_exc:
            self._handle_retryable_poll_error(rate_exc, backoff, mgr)
        except Exception:
            pass
