"""
DEMO Mode Readiness Diagnostic System.

Validates credentials, streaming, execution routing, and order path before DEMO trading.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from execution.execution_engine import ExecutionEngine
from execution.trading_loop import TradingLoop as ExecutionTickLoop
from execution.types import ExecutionMode
from ig_api.mock_clients import MockIGRest
from ig_api.streaming_client import ConnectionState, IGStreamingClient, PriceUpdate
from system.config_loader import get_config
from system.credentials_loader import (
    Credentials,
    credentials_path,
    try_load_credentials,
)
from system.demo_readiness_log import log_demo_readiness
from system.engine_log import log_engine


@dataclass
class ReadinessCheckResult:
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DemoReadinessReport:
    ready: bool
    checks: list[ReadinessCheckResult] = field(default_factory=list)

    def failures(self) -> list[ReadinessCheckResult]:
        return [c for c in self.checks if not c.ok]

    def failure_summary(self) -> str:
        return "; ".join(f"{c.name}: {c.message}" for c in self.failures()) or "unknown"


_last_report: DemoReadinessReport | None = None
_readiness_cache: tuple[float, DemoReadinessReport] | None = None
_READINESS_CACHE_TTL_SEC = 300.0


def get_last_demo_readiness_report() -> DemoReadinessReport | None:
    return _last_report


def check_demo_credentials_ready() -> ReadinessCheckResult:
    """Section 1 — credentials.json and required DEMO fields."""
    name = "credentials"
    path = credentials_path()
    if not path.is_file():
        return ReadinessCheckResult(
            name, False, f"credentials.json not found at {path}", {"path": str(path)}
        )

    status = try_load_credentials(path=path)
    if not status.ok or not status.credentials:
        return ReadinessCheckResult(name, False, status.error or "Invalid credentials", {})

    creds = status.credentials
    missing: list[str] = []
    for fld, val in (
        ("ig_api_key", creds.ig_api_key),
        ("ig_username", creds.ig_username),
        ("ig_password", creds.ig_password),
        ("ig_account_type", creds.ig_account_type),
        ("ig_account_id", creds.ig_account_id),
    ):
        if not str(val).strip():
            missing.append(fld)

    if missing:
        return ReadinessCheckResult(
            name, False, f"Missing fields: {', '.join(missing)}", {"missing": missing}
        )

    if creds.account_type != "DEMO":
        return ReadinessCheckResult(
            name,
            False,
            f"ig_account_type must be DEMO (got {creds.account_type})",
            {"account_type": creds.account_type},
        )

    log_demo_readiness("DEMO credentials validated")
    return ReadinessCheckResult(
        name,
        True,
        "DEMO credentials validated",
        {"account_id": creds.masked_account_id(), "path": str(path)},
    )


def _market_closed_streaming_skip(epic: str) -> ReadinessCheckResult | None:
    """When the configured epic's market is closed, skip live tick probes."""
    from system.market_watch.calendar import get_market_status

    mkt = get_market_status(epic)
    if mkt is None or mkt.open:
        return None
    msg = f"Market closed — probe skipped ({mkt.reason})"
    log_demo_readiness(f"DEMO streaming deferred — {msg}")
    log_engine(f"Startup ig_streaming: deferred — market closed ({mkt.reason})")
    return ReadinessCheckResult(
        "streaming",
        True,
        msg,
        {
            "market_closed": True,
            "epic": epic,
            "reason": mkt.reason,
            "next_open": mkt.next_open_at.isoformat() if mkt.next_open_at else "",
        },
    )


def _probe_demo_streaming(
    creds: Credentials,
    epic: str,
    *,
    timeout_seconds: float = 30.0,
) -> ReadinessCheckResult:
    """Connect to IG DEMO streaming (REST poll) and wait for tick + heartbeat."""
    from system.ig_rest_session import ensure_shared_authenticated

    name = "streaming"
    try:
        rest = ensure_shared_authenticated(creds)
    except Exception as e:
        return ReadinessCheckResult(name, False, f"REST login failed: {e}", {})

    if "demo-api.ig.com" not in getattr(rest, "_base", ""):
        return ReadinessCheckResult(
            name,
            False,
            f"Not using DEMO REST endpoint: {rest._base}",
            {"base": rest._base},
        )

    session = rest.session
    if not session or not session.is_valid:
        return ReadinessCheckResult(name, False, "Invalid session after login", {})

    price_event = threading.Event()
    heartbeat_event = threading.Event()
    last_price: dict[str, float] = {}
    states: list[str] = []

    def on_price(update: PriceUpdate) -> None:
        last_price["bid"] = update.bid
        last_price["offer"] = update.offer
        price_event.set()

    def on_account(_update: Any) -> None:
        heartbeat_event.set()

    def on_state(state: ConnectionState) -> None:
        states.append(state.value)

    poll = float(get_config(reload=False).stream_poll_seconds)
    stream = IGStreamingClient(creds, session, rest_client=rest, poll_interval_seconds=poll)
    stream.on_price(on_price)
    stream.on_account(on_account)
    stream.on_state_change(on_state)
    stream.subscribe_market(epic)

    try:
        stream.connect()
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if price_event.is_set():
                break
            time.sleep(0.2)

        if not price_event.is_set():
            msg = f"No price ticks within {timeout_seconds:.0f}s"
            log_engine(f"Startup ig_streaming: FAIL — {msg} epic={epic}")
            return ReadinessCheckResult(
                name,
                False,
                msg,
                {"states": states, "epic": epic},
            )

        hb_deadline = time.time() + min(4.0, timeout_seconds / 2)
        while time.time() < hb_deadline and not heartbeat_event.is_set():
            time.sleep(0.2)

        connected = stream.state == ConnectionState.CONNECTED
        if not connected:
            return ReadinessCheckResult(
                name,
                False,
                f"Stream not connected (state={stream.state.value})",
                {"states": states},
            )

        log_demo_readiness("DEMO streaming connected")
        from system.market_watch.calendar import confirm_market_open_stream_live

        confirm_market_open_stream_live()
        return ReadinessCheckResult(
            name,
            True,
            "DEMO streaming connected",
            {
                "bid": last_price.get("bid"),
                "offer": last_price.get("offer"),
                "heartbeat": heartbeat_event.is_set(),
                "states": states,
                "epic": epic,
            },
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Streaming probe failed: {e}", {"states": states})
    finally:
        stream.disconnect()


def check_demo_streaming_ready(
    *,
    bot: Any | None = None,
    epic: str | None = None,
    probe_if_needed: bool = True,
    timeout_seconds: float = 30.0,
) -> ReadinessCheckResult:
    """Section 2 — IG DEMO streaming connectivity and ticks."""
    cred_check = check_demo_credentials_ready()
    if not cred_check.ok:
        return ReadinessCheckResult(
            "streaming", False, f"Credentials not ready: {cred_check.message}", {}
        )

    cfg = get_config()
    epic = epic or cfg.epic
    creds = try_load_credentials().credentials
    assert creds is not None

    closed = _market_closed_streaming_skip(epic)
    if closed is not None:
        return closed

    if bot is not None:
        stream = getattr(bot, "_stream", None) or getattr(bot, "_stream_client", None)
        if stream is not None:
            st = getattr(stream, "state", None)
            st_val = getattr(st, "value", str(st))
            if st_val == "connected" or st_val == ConnectionState.CONNECTED.value:
                log_demo_readiness("DEMO streaming connected (active stream)")
                from system.market_watch.calendar import confirm_market_open_stream_live

                confirm_market_open_stream_live()
                return ReadinessCheckResult(
                    "streaming",
                    True,
                    "DEMO streaming connected (active stream)",
                    {"state": st_val},
                )

    if not probe_if_needed:
        return ReadinessCheckResult(
            "streaming", False, "Streaming not connected", {"probe_skipped": True}
        )

    return _probe_demo_streaming(creds, epic, timeout_seconds=timeout_seconds)


def _execution_loop_from_bot(bot: Any) -> ExecutionTickLoop | None:
    loop = getattr(bot, "_execution_loop", None) or getattr(bot, "execution_loop", None)
    if isinstance(loop, ExecutionTickLoop):
        return loop
    inner = getattr(bot, "trading_loop", None)
    if inner is not None:
        return _execution_loop_from_bot(inner)
    return None


def check_demo_execution_ready(
    *,
    bot: Any | None = None,
    rest_client: Any | None = None,
) -> ReadinessCheckResult:
    """Section 3 — execution engine in DEMO mode (not simulator)."""
    name = "execution"

    if bot is not None:
        tick_loop = _execution_loop_from_bot(bot)
        if tick_loop is not None:
            engine: ExecutionEngine = tick_loop.execution_engine
            mode = engine.mode
            if mode != ExecutionMode.DEMO:
                return ReadinessCheckResult(
                    name,
                    False,
                    f"Bot not in DEMO mode (mode={getattr(mode, 'value', mode)})",
                    {},
                )
            if engine.mode.uses_simulator():
                return ReadinessCheckResult(name, False, "Execution engine using simulator", {})
            if engine._live is None:
                return ReadinessCheckResult(name, False, "LiveExecutor not initialised", {})

            components = {
                "signal_engine": tick_loop.signal_engine is not None,
                "adaptive_engine": hasattr(engine, "_adaptive"),
                "risk_manager": hasattr(engine, "_risk"),
                "order_validator": hasattr(engine, "_validator"),
                "live_executor": engine._live is not None,
            }
            if not all(components.values()):
                missing = [k for k, v in components.items() if not v]
                return ReadinessCheckResult(
                    name, False, f"Missing components: {', '.join(missing)}", components
                )

            log_demo_readiness("DEMO execution engine active")
            return ReadinessCheckResult(
                name,
                True,
                "DEMO execution engine active (broker path, not simulator)",
                components,
            )

    cred_check = check_demo_credentials_ready()
    if not cred_check.ok:
        return ReadinessCheckResult(name, False, cred_check.message, {})

    try:
        from ig_api.rest_client import IGRestClient
        import tempfile
        from pathlib import Path

        from data.learning_store import LearningStore

        cfg = get_config()
        if rest_client is not None:
            rest = rest_client
        else:
            creds = try_load_credentials().credentials
            assert creds is not None
            rest = IGRestClient(creds)
            rest.login()

        db = Path(tempfile.mkdtemp()) / "readiness.db"
        store = LearningStore(str(db))
        store.connect()

        has_pos = getattr(rest, "has_open_position", None)
        engine = ExecutionEngine(
            mode=ExecutionMode.DEMO,
            config=cfg,
            store=store,
            rest_client=rest,
            has_broker_position=has_pos,
        )
        if engine.mode.uses_simulator():
            return ReadinessCheckResult(name, False, "Ephemeral engine in simulator mode", {})
        if engine._live is None:
            return ReadinessCheckResult(name, False, "LiveExecutor missing on ephemeral engine", {})

        log_demo_readiness("DEMO execution engine active (pre-flight)")
        return ReadinessCheckResult(
            name,
            True,
            "DEMO execution engine ready (pre-flight)",
            {"broker_mode": True, "simulator": False},
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Execution pre-flight failed: {e}", {})


def check_demo_order_routing_ready(
    *,
    epic: str | None = None,
    rest_client: Any | None = None,
    market_bid: float | None = None,
    market_offer: float | None = None,
    skip_balance_check: bool = False,
) -> ReadinessCheckResult:
    """Section 4 — DEMO REST endpoints, account ID, dry-run routing validation."""
    name = "order_routing"
    cred_check = check_demo_credentials_ready()
    if not cred_check.ok:
        return ReadinessCheckResult(name, False, cred_check.message, {})

    cfg = get_config()
    epic = epic or cfg.epic

    try:
        from ig_api.rest_client import IGRestClient

        creds = try_load_credentials().credentials
        assert creds is not None

        if rest_client is not None:
            rest = rest_client
        else:
            rest = IGRestClient(creds)
            rest.login()

        if isinstance(rest, MockIGRest) or "demo-api.ig.com" not in rest._base:
            return ReadinessCheckResult(
                name,
                False,
                "REST client is not configured for IG DEMO endpoints",
                {"base": getattr(rest, "_base", "")},
            )

        if rest.account_id != creds.ig_account_id:
            return ReadinessCheckResult(
                name,
                False,
                "REST account_id does not match credentials",
                {"rest": rest.account_id, "credentials": creds.masked_account_id()},
            )

        validation = rest.validate_demo_order_routing(
            epic=epic,
            dry_run=True,
            market_bid=market_bid,
            market_offer=market_offer,
            skip_balance_check=skip_balance_check,
        )
        if not validation.get("ok"):
            return ReadinessCheckResult(
                name,
                False,
                validation.get("error", "Order routing validation failed"),
                validation,
            )

        if validation.get("is_mock"):
            return ReadinessCheckResult(
                name, False, "Order routing points to mock client (simulator)", validation
            )

        log_demo_readiness("DEMO order routing ready")
        return ReadinessCheckResult(
            name,
            True,
            "DEMO order routing ready (dry-run validated, no order placed)",
            validation,
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Order routing check failed: {e}", {})


def check_rate_limit_ready() -> ReadinessCheckResult:
    """Block readiness while IG API rate limit cooldown is active."""
    from system.rate_limit_manager import get_rate_limit_manager

    mgr = get_rate_limit_manager()
    if not mgr.is_active():
        return ReadinessCheckResult("rate_limit", True, "No rate limit active")
    remaining = int(mgr.seconds_until_rest_reset())
    return ReadinessCheckResult(
        "rate_limit",
        False,
        f"IG API rate limit — wait {remaining // 60}m {remaining % 60}s",
        {"backoff_stage": mgr.snapshot().backoff_stage},
    )


def run_demo_mode_readiness_check(
    *,
    bot: Any | None = None,
    epic: str | None = None,
    probe_streaming: bool = True,
    streaming_timeout: float = 30.0,
    force_refresh: bool = False,
) -> DemoReadinessReport:
    """Master DEMO readiness — delegates to startup pipeline."""
    global _last_report, _readiness_cache

    if not force_refresh and _readiness_cache is not None:
        cached_at, cached = _readiness_cache
        if time.time() - cached_at < _READINESS_CACHE_TTL_SEC and cached.ready:
            log_demo_readiness("--- DEMO readiness (cached) ---")
            return cached

    from system.startup_pipeline import run_startup_pipeline

    startup = run_startup_pipeline(
        epic=epic,
        probe_streaming=probe_streaming,
        streaming_timeout=streaming_timeout,
        bot=bot,
        force_refresh=force_refresh,
    )
    ready = startup.ready
    report = DemoReadinessReport(ready=ready, checks=list(startup.checks))
    _last_report = report
    _readiness_cache = (time.time(), report)

    for c in report.checks:
        status = "PASS" if c.ok else "FAIL"
        log_demo_readiness(f"[{status}] {c.name}: {c.message}")

    if ready:
        log_demo_readiness("DEMO mode fully ready")
    else:
        log_demo_readiness(f"DEMO mode NOT ready — {report.failure_summary()}")

    return report
