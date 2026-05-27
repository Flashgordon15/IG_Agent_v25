"""
Deterministic startup pipeline — config, credentials, IG connectivity, diagnostics.

Does not place trades. IG state remains authoritative during runtime via position sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from system.config_loader import get_config
from system.credentials_loader import (
    Credentials,
    credentials_path,
    try_load_credentials,
)
from system.demo_readiness import (
    DemoReadinessReport,
    ReadinessCheckResult,
    check_demo_credentials_ready,
    check_demo_execution_ready,
    check_demo_streaming_ready,
    check_rate_limit_ready,
)
from system.demo_readiness_log import log_demo_readiness
from system.engine_log import log_engine

STARTUP_STATUS_READY = "READY"
STARTUP_STATUS_NOT_READY = "NOT READY"

# Display names for Startup Diagnostics panel
PANEL_CHECKS = (
    ("credentials", "Credentials loaded"),
    ("ig_rest_auth", "IG REST authentication"),
    ("ig_streaming", "IG streaming"),
    ("market_subscription", "Market subscription"),
    ("account_balance", "Account balance"),
    ("position_sync", "Position sync"),
    ("order_routing", "Order routing (DEMO)"),
    ("execution_engine", "Execution engine"),
)


@dataclass
class StartupReport:
    ready: bool
    status: str
    checks: list[ReadinessCheckResult] = field(default_factory=list)
    failure_summary: str = ""

    def as_demo_report(self) -> DemoReadinessReport:
        return DemoReadinessReport(ready=self.ready, checks=self.checks)

    def check_map(self) -> dict[str, ReadinessCheckResult]:
        return {c.name: c for c in self.checks}

    def panel_rows(self) -> list[tuple[str, str, str]]:
        """(label, PASS/FAIL, message) for UI."""
        cmap = self.check_map()
        rows: list[tuple[str, str, str]] = []
        for key, label in PANEL_CHECKS:
            c = cmap.get(key)
            if c is None:
                rows.append((label, "—", "Not run"))
            else:
                rows.append((label, "PASS" if c.ok else "FAIL", c.message))
        return rows


_last_startup_report: StartupReport | None = None
_startup_cache: tuple[float, StartupReport] | None = None
_CACHE_TTL_SEC = 300.0


def get_last_startup_report() -> StartupReport | None:
    return _last_startup_report


def check_config_loaded() -> ReadinessCheckResult:
    name = "config"
    try:
        cfg = get_config(reload=False)
        if not cfg.epic:
            return ReadinessCheckResult(name, False, "Config epic is empty", {})
        return ReadinessCheckResult(
            name,
            True,
            f"Config loaded — epic={cfg.epic}",
            {"epic": cfg.epic, "account_type": cfg.account_type},
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Config load failed: {e}", {})


def check_ig_rest_authentication(creds: Credentials) -> tuple[ReadinessCheckResult, Any | None]:
    """Authenticate once; return (check, rest_client or None)."""
    from system.ig_rest_session import ensure_shared_authenticated

    name = "ig_rest_auth"
    try:
        rest = ensure_shared_authenticated(creds)
        base = getattr(rest, "_base", "")
        if creds.account_type == "DEMO" and "demo-api.ig.com" not in base:
            return (
                ReadinessCheckResult(name, False, f"Wrong REST base for DEMO: {base}", {}),
                None,
            )
        session = rest.session
        if not session or not session.is_valid:
            return ReadinessCheckResult(name, False, "Session invalid after login", {}), None
        return (
            ReadinessCheckResult(
                name,
                True,
                f"Authenticated — account {creds.masked_account_id()}",
                {"base": base, "account_id": rest.account_id},
            ),
            rest,
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, str(e), {}), None


def check_account_type_demo(creds: Credentials) -> ReadinessCheckResult:
    name = "account_type"
    if creds.account_type != "DEMO":
        return ReadinessCheckResult(
            name,
            False,
            f"Expected DEMO account type (got {creds.account_type})",
            {},
        )
    return ReadinessCheckResult(name, True, "Account type DEMO confirmed", {})


def check_market_subscription(rest: Any, epic: str) -> ReadinessCheckResult:
    name = "market_subscription"
    try:
        from system.market_watch.calendar import get_market_status

        status = get_market_status(epic)
        if status and not status.open:
            return ReadinessCheckResult(
                name,
                True,
                f"Market closed — probe skipped ({status.reason})",
                {
                    "epic": epic,
                    "market_closed": True,
                    "next_open": status.next_open_at.isoformat() if status.next_open_at else "",
                },
            )
        snap = rest.fetch_market_snapshot(epic)
        bid = float(snap.get("bid") or snap.get("snapshot", {}).get("bid") or 0)
        offer = float(snap.get("offer") or snap.get("snapshot", {}).get("offer") or 0)
        if bid <= 0 or offer <= 0:
            return ReadinessCheckResult(
                name, False, f"Invalid prices bid={bid} offer={offer}", snap
            )
        return ReadinessCheckResult(
            name,
            True,
            f"Market {epic} subscribed — {bid:.1f}/{offer:.1f}",
            {"bid": bid, "offer": offer, "epic": epic},
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Market probe failed: {e}", {})


def check_account_balance(rest: Any) -> ReadinessCheckResult:
    name = "account_balance"
    try:
        bal = rest.fetch_account_balance()
        cached = rest.get_cached_account_summary() if hasattr(rest, "get_cached_account_summary") else {}
        return ReadinessCheckResult(
            name,
            True,
            f"Balance available: {bal:.2f}",
            {"balance": bal, **cached},
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Balance check failed: {e}", {})


def check_position_sync_probe(rest: Any) -> ReadinessCheckResult:
    name = "position_sync"
    try:
        positions = rest.open_positions()
        n = len([p for p in positions if float((p.get("position") or {}).get("size") or 0) > 0])
        return ReadinessCheckResult(
            name,
            True,
            f"GET /positions OK — {n} open",
            {"open_count": n},
        )
    except Exception as e:
        return ReadinessCheckResult(name, False, f"Position sync probe failed: {e}", {})


def run_startup_pipeline(
    *,
    epic: str | None = None,
    probe_streaming: bool = True,
    streaming_timeout: float = 30.0,
    bot: Any | None = None,
    force_refresh: bool = False,
) -> StartupReport:
    """
    Mandatory startup sequence for DEMO trading readiness.
    """
    global _last_startup_report, _startup_cache

    if not force_refresh and _startup_cache is not None:
        cached_at, cached = _startup_cache
        if time.time() - cached_at < _CACHE_TTL_SEC:
            return cached

    log_demo_readiness("--- Startup pipeline started ---")
    log_engine("Startup pipeline: begin")

    checks: list[ReadinessCheckResult] = []
    checks.append(check_config_loaded())
    checks.append(check_rate_limit_ready())

    cred_result = check_demo_credentials_ready()
    checks.append(cred_result)

    rate_ok = checks[-2].ok if len(checks) >= 2 else True
    if not rate_ok or not cred_result.ok:
        checks.extend(
            _skipped_checks(
                ("ig_rest_auth", "ig_streaming", "market_subscription", "account_balance", "position_sync", "order_routing", "execution_engine"),
                "Skipped — prerequisites failed",
            )
        )
        return _finalize(checks)

    creds = try_load_credentials(path=credentials_path()).credentials
    assert creds is not None
    checks.append(check_account_type_demo(creds))

    cfg = get_config()
    epic = epic or cfg.epic

    auth_result, rest = check_ig_rest_authentication(creds)
    checks.append(auth_result)

    if not auth_result.ok or rest is None:
        checks.extend(
            _skipped_checks(
                ("market_subscription", "account_balance", "position_sync", "order_routing", "execution_engine"),
                "Skipped — REST auth failed",
            )
        )
        checks.append(
            ReadinessCheckResult("ig_streaming", False, "Skipped — REST auth failed", {})
        )
        return _finalize(checks)

    checks.append(check_market_subscription(rest, epic))
    market_check = checks[-1]
    checks.append(check_account_balance(rest))
    checks.append(check_position_sync_probe(rest))

    from system.demo_readiness import check_demo_order_routing_ready

    mkt_bid = float(market_check.details.get("bid") or 0) if market_check.ok else None
    mkt_offer = float(market_check.details.get("offer") or 0) if market_check.ok else None
    checks.append(
        check_demo_order_routing_ready(
            epic=epic,
            rest_client=rest,
            market_bid=mkt_bid,
            market_offer=mkt_offer,
            skip_balance_check=True,
        )
    )

    checks.append(
        _check_ig_streaming(
            epic=epic,
            probe_streaming=probe_streaming,
            bot=bot,
            streaming_timeout=streaming_timeout,
        )
    )

    exec_check = check_demo_execution_ready(bot=bot, rest_client=rest)
    checks.append(
        ReadinessCheckResult(
            "execution_engine",
            exec_check.ok,
            exec_check.message,
            exec_check.details,
        )
    )

    return _finalize(checks)


def _check_ig_streaming(
    *,
    epic: str,
    probe_streaming: bool,
    bot: Any | None,
    streaming_timeout: float,
) -> ReadinessCheckResult:
    """IG streaming readiness — defer tick wait when the market is closed."""
    from system.demo_readiness import (
        _market_closed_streaming_skip,
        check_demo_streaming_ready,
    )

    closed = _market_closed_streaming_skip(epic)
    if closed is not None:
        return ReadinessCheckResult(
            "ig_streaming",
            closed.ok,
            closed.message,
            closed.details,
        )

    if not probe_streaming:
        return ReadinessCheckResult(
            "ig_streaming",
            True,
            "Deferred — preview feed validates streaming after checks",
            {},
        )

    stream = check_demo_streaming_ready(
        bot=bot,
        epic=epic,
        probe_if_needed=True,
        timeout_seconds=streaming_timeout,
    )
    return ReadinessCheckResult(
        "ig_streaming",
        stream.ok,
        stream.message,
        stream.details,
    )


def _skipped_checks(names: tuple[str, ...], msg: str) -> list[ReadinessCheckResult]:
    return [ReadinessCheckResult(n, False, msg, {}) for n in names]


def _finalize(checks: list[ReadinessCheckResult]) -> StartupReport:
    global _last_startup_report, _startup_cache

    ready = all(c.ok for c in checks)
    failures = [c for c in checks if not c.ok]
    summary = "; ".join(f"{c.name}: {c.message}" for c in failures) if failures else ""

    report = StartupReport(
        ready=ready,
        status=STARTUP_STATUS_READY if ready else STARTUP_STATUS_NOT_READY,
        checks=checks,
        failure_summary=summary,
    )
    _last_startup_report = report
    _startup_cache = (time.time(), report)

    for c in checks:
        log_demo_readiness(f"[{'PASS' if c.ok else 'FAIL'}] startup.{c.name}: {c.message}")

    if ready:
        try:
            from system.startup_countdown import persist_startup_ready_stamp

            persist_startup_ready_stamp()
        except Exception:
            pass
        log_engine("Startup pipeline: READY — DEMO trading may be enabled")
        log_demo_readiness("Startup pipeline READY")
    else:
        log_engine(f"Startup pipeline: NOT READY — {summary}")
        log_demo_readiness(f"Startup pipeline NOT READY — {summary}")

    return report
