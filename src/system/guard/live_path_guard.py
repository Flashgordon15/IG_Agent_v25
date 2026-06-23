"""Live PRODUCTION track — absolute mock excision (fail-closed ``sys.exit(101)``)."""

from __future__ import annotations

import os
import sys

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.guard.security_errors import FailClosedSecurityError


def is_live_production_track() -> bool:
    """True when Live Vanguard runs under PRODUCTION with no mock fallback permitted."""
    track = os.environ.get("IG_PARALLEL_TRACK", "").strip().lower()
    mode = os.environ.get("IG_APEX_RUNTIME_MODE", "").strip().upper()
    return track == "live" and mode == "PRODUCTION"


def mock_broker_forbidden() -> bool:
    """True when mock REST / mock feed paths must never be installed."""
    if is_live_production_track():
        return True
    try:
        from system.agent_execution_mode import (
            authentic_demo_broker_required,
            production_execution_active,
        )

        return production_execution_active() or authentic_demo_broker_required()
    except Exception:
        return False


def enforce_live_production_no_mock(context: str) -> None:
    """
    Hard stop — mock clients, MockIGRest, and MockFeedEngine are forbidden on live PRODUCTION.

    Never returns on violation (``sys.exit(101)``).
    """
    if not mock_broker_forbidden():
        return
    message = (
        "FailClosedSecurityError: Live PRODUCTION track forbids mock/simulated "
        f"broker paths — {context}"
    )
    exc = FailClosedSecurityError(message)
    log_engine(message)
    log_guarded_exception("live_production_mock_excision", exc, detail=context)
    sys.exit(101)


def block_mock_client_factory(context: str) -> None:
    """Alias for factory entry points (MockIGRest / MockFeedEngine)."""
    enforce_live_production_no_mock(context)
