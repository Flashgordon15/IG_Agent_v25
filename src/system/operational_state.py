"""
Unified operational state for GUI banner, status strip, and control enablement.

Single source of truth so banner text never contradicts MODE / ERRORS display.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from system.rate_limit_manager import get_rate_limit_manager


class OperationalState(str, Enum):
    IDLE = "idle"
    STARTUP_COUNTDOWN = "startup_countdown"
    STARTUP_CHECKS = "startup_checks"
    PREVIEW_READY = "preview_ready"
    MARKET_CLOSED = "market_closed"
    NOT_READY = "not_ready"
    DEMO_RUNNING = "demo_running"
    TEST_RUNNING = "test_running"
    LIVE_RUNNING = "live_running"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class OperationalSnapshot:
    state: OperationalState
    banner_title: str
    banner_detail: str
    banner_style: str  # ok | warn | err | neutral
    mode_label: str
    error_line: str
    broker_enabled: bool
    verify_enabled: bool


def _market_closed_snapshot(
    *,
    demo_ready: bool,
    market_label: str,
    detail: str,
) -> OperationalSnapshot:
    if demo_ready:
        return OperationalSnapshot(
            state=OperationalState.MARKET_CLOSED,
            banner_title=f"{market_label} closed — Start DEMO for warmup",
            banner_detail=f"{detail} · Live quotes resume when the session opens",
            banner_style="warn",
            mode_label="READY",
            error_line="None",
            broker_enabled=True,
            verify_enabled=False,
        )
    return OperationalSnapshot(
        state=OperationalState.MARKET_CLOSED,
        banner_title=f"{market_label} closed",
        banner_detail=detail,
        banner_style="neutral",
        mode_label="MARKET CLOSED",
        error_line="None",
        broker_enabled=False,
        verify_enabled=False,
    )


def resolve_operational_state(
    *,
    bot_running: bool,
    bot_mode: str,
    market_label: str = "Japan 225",
    epic: str = "",
    demo_ready: bool = False,
    readiness_running: bool = False,
    startup_countdown: bool = False,
    startup_remaining: int = 0,
    not_ready_detail: str = "",
    sync_error: str = "",
    bot_error: str = "",
) -> OperationalSnapshot:
    market_status = None
    if epic:
        try:
            from system.market_watch.calendar import get_market_status

            market_status = get_market_status(epic)
        except Exception:
            market_status = None

    mgr = get_rate_limit_manager()
    if mgr.is_active():
        countdown = mgr.format_countdown()
        snap = mgr.snapshot()
        return OperationalSnapshot(
            state=OperationalState.RATE_LIMITED,
            banner_title="IG API rate limit — trading paused",
            banner_detail=(
                f"Reset in {countdown} | Stage {snap.backoff_stage} | "
                "Do not restart until countdown ends"
            ),
            banner_style="err",
            mode_label="RATE LIMITED",
            error_line=f"REST paused ({countdown})",
            broker_enabled=False,
            verify_enabled=False,
        )

    mode = str(bot_mode or "OFF").upper()

    if bot_running:
        if "DEMO" in mode:
            if market_status and not market_status.open:
                return OperationalSnapshot(
                    state=OperationalState.DEMO_RUNNING,
                    banner_title=f"DEMO running — {market_label}",
                    banner_detail=(
                        f"{market_status.message} · IG REST sync active · "
                        "stream deferred until session opens"
                    ),
                    banner_style="ok",
                    mode_label="DEMO",
                    error_line=_compose_error(sync_error, bot_error),
                    broker_enabled=False,
                    verify_enabled=True,
                )
            return OperationalSnapshot(
                state=OperationalState.DEMO_RUNNING,
                banner_title=f"DEMO running — {market_label}",
                banner_detail="Live IG stream and sync active",
                banner_style="ok",
                mode_label="DEMO",
                error_line=_compose_error(sync_error, bot_error),
                broker_enabled=False,
                verify_enabled=True,
            )
        if mode == "TEST" or "TEST" in mode:
            return OperationalSnapshot(
                state=OperationalState.TEST_RUNNING,
                banner_title=f"TEST running — {market_label}",
                banner_detail="Simulator mode — no broker orders",
                banner_style="ok",
                mode_label="TEST RUNNING",
                error_line=_compose_error(sync_error, bot_error),
                broker_enabled=False,
                verify_enabled=False,
            )
        if mode == "LIVE" or "LIVE" in mode:
            return OperationalSnapshot(
                state=OperationalState.LIVE_RUNNING,
                banner_title=f"LIVE running — {market_label}",
                banner_detail="Real capital — IG broker active",
                banner_style="ok",
                mode_label="LIVE RUNNING",
                error_line=_compose_error(sync_error, bot_error),
                broker_enabled=False,
                verify_enabled=True,
            )

    if readiness_running:
        return OperationalSnapshot(
            state=OperationalState.STARTUP_CHECKS,
            banner_title="Running startup checks…",
            banner_detail="Authenticating and probing IG DEMO connectivity",
            banner_style="warn",
            mode_label="CHECKS",
            error_line="None",
            broker_enabled=False,
            verify_enabled=False,
        )

    if demo_ready:
        if market_status and not market_status.open:
            return _market_closed_snapshot(
                demo_ready=True,
                market_label=market_label,
                detail=market_status.message,
            )
        return OperationalSnapshot(
            state=OperationalState.PREVIEW_READY,
            banner_title="All checks passed — click Start DEMO Mode to trade",
            banner_detail=f"Preview feed active · {market_label}",
            banner_style="ok",
            mode_label="READY",
            error_line="None",
            broker_enabled=True,
            verify_enabled=False,
        )

    if startup_countdown and startup_remaining >= 0:
        sec = max(0, int(startup_remaining))
        return OperationalSnapshot(
            state=OperationalState.STARTUP_COUNTDOWN,
            banner_title=f"Preparing startup checks — {sec}s",
            banner_detail="Waiting before IG connectivity checks (API quota settle)",
            banner_style="warn",
            mode_label="STARTING",
            error_line="None",
            broker_enabled=False,
            verify_enabled=False,
        )

    if not_ready_detail:
        return OperationalSnapshot(
            state=OperationalState.NOT_READY,
            banner_title="DEMO mode NOT READY — see Diagnostics",
            banner_detail=not_ready_detail[:200],
            banner_style="err",
            mode_label="NOT READY",
            error_line="Startup checks failed",
            broker_enabled=False,
            verify_enabled=False,
        )

    if market_status and not market_status.open:
        return _market_closed_snapshot(
            demo_ready=False,
            market_label=market_label,
            detail=market_status.message,
        )

    return OperationalSnapshot(
        state=OperationalState.IDLE,
        banner_title="IG Agent ready — load credentials and start",
        banner_detail=market_label,
        banner_style="neutral",
        mode_label="OFF",
        error_line="None",
        broker_enabled=False,
        verify_enabled=False,
    )


def _compose_error(sync_error: str, bot_error: str) -> str:
    for part in (sync_error.strip(), bot_error.strip()):
        if part:
            return part[:60]
    return "None"
