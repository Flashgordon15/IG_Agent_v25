"""
Apex process-wide runtime plane — distinct from ``execution.types.ExecutionMode``.

ExecutionMode (TEST / DEMO / LIVE) selects order routing inside the engine.
ApexRuntimeMode selects the **host environment** and data/network isolation:

  PRODUCTION       — live :8080 host, production ledger paths
  SHADOW_LIVE      — desktop :9090 shadow sidecar (isolated v30 namespace)
  HARDENED_TESTBED — deterministic replay; zero production I/O (see testbed_firewall)
"""

from __future__ import annotations

import os
from enum import Enum

from system.engine_log import log_engine


class ApexRuntimeMode(str, Enum):
    PRODUCTION = "PRODUCTION"
    SHADOW_LIVE = "SHADOW_LIVE"
    HARDENED_TESTBED = "HARDENED_TESTBED"

    @property
    def is_testbed(self) -> bool:
        return self is ApexRuntimeMode.HARDENED_TESTBED

    @property
    def is_shadow(self) -> bool:
        return self is ApexRuntimeMode.SHADOW_LIVE

    @property
    def is_production(self) -> bool:
        return self is ApexRuntimeMode.PRODUCTION


_MODE: ApexRuntimeMode | None = None


def _desktop_shell() -> bool:
    return os.environ.get("IG_APEX_DESKTOP", "").strip() == "1" or (
        os.environ.get("IG_AGENT_DESKTOP_LAUNCH", "").strip() == "1"
    )


def resolve_apex_runtime_mode(*, reload: bool = False) -> ApexRuntimeMode:
    """Resolve runtime plane from ``IG_APEX_RUNTIME_MODE`` with safe defaults."""
    global _MODE
    if _MODE is not None and not reload:
        return _MODE

    raw = os.environ.get("IG_APEX_RUNTIME_MODE", "").strip().upper()
    if raw in ("HARDENED_TESTBED", "TESTBED", "REPLAY"):
        _MODE = ApexRuntimeMode.HARDENED_TESTBED
        return _MODE
    if raw in ("SHADOW_LIVE", "SHADOW"):
        _MODE = ApexRuntimeMode.SHADOW_LIVE
        return _MODE
    if raw in ("PRODUCTION", "LIVE", "PROD"):
        _MODE = ApexRuntimeMode.PRODUCTION
        return _MODE

    if _desktop_shell():
        _MODE = ApexRuntimeMode.SHADOW_LIVE
        return _MODE

    profile = (
        os.environ.get("IG_NODE_PROFILE", "").strip()
        or os.environ.get("NODE_ENV", "").strip()
        or "production"
    ).lower()
    if profile in ("shadow", "v30", "sandbox", "development"):
        _MODE = ApexRuntimeMode.SHADOW_LIVE
    else:
        _MODE = ApexRuntimeMode.PRODUCTION
    return _MODE


def get_apex_runtime_mode() -> ApexRuntimeMode:
    return resolve_apex_runtime_mode()


def reset_apex_runtime_mode_for_tests() -> None:
    global _MODE
    _MODE = None


def apply_runtime_mode_to_environ() -> ApexRuntimeMode:
    """Publish runtime plane to os.environ — call before node_profile / data paths."""
    mode = resolve_apex_runtime_mode(reload=True)
    os.environ["IG_APEX_RUNTIME_MODE"] = mode.value

    if mode.is_testbed:
        from system.testbed_firewall import arm_testbed_firewall

        arm_testbed_firewall()
        log_engine(
            "ApexRuntimeMode=HARDENED_TESTBED — production firewall armed, "
            "loopback transport only"
        )
    elif mode.is_shadow:
        os.environ.setdefault("IG_NODE_PROFILE", "shadow")
        log_engine("ApexRuntimeMode=SHADOW_LIVE — isolated shadow namespace")
    else:
        log_engine("ApexRuntimeMode=PRODUCTION — production data plane")

    return mode
