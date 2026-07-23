"""
v31 APP_MODE — operator-facing runtime mode (DEMO / LIVE / TESTBED).

Derives legacy plane selectors (ApexRuntimeMode, NodeProfile, IG_AGENT_MODE)
from a single APP_MODE authority. Call ``apply_app_mode_to_environ()`` immediately
after ``prepare_boot_env()`` and before ``apply_runtime_mode_to_environ()``.
"""

from __future__ import annotations

import os
import sys
from enum import Enum

from system.engine_log import log_engine

_VALID = frozenset({"DEMO", "LIVE", "TESTBED"})
_LIVE_ARM_VALUES = frozenset({"1", "true", "yes", "on"})
_MODE: AppMode | None = None
_SHIM_WARNED = False


class AppMode(str, Enum):
    DEMO = "DEMO"
    LIVE = "LIVE"
    TESTBED = "TESTBED"


def parse_app_mode(raw: str | None) -> AppMode:
    """Parse and validate APP_MODE string — raises ValueError when invalid."""
    if raw is None or not str(raw).strip():
        raise ValueError("APP_MODE is required (DEMO, LIVE, or TESTBED)")
    key = str(raw).strip().upper()
    if key not in _VALID:
        raise ValueError(f"invalid APP_MODE {raw!r} — expected DEMO, LIVE, or TESTBED")
    return AppMode(key)


def _legacy_shim_to_app_mode() -> AppMode | None:
    """Map deprecated env selectors to APP_MODE (transition shim)."""
    apex = os.environ.get("IG_APEX_RUNTIME_MODE", "").strip().upper()
    if apex in ("HARDENED_TESTBED", "TESTBED", "REPLAY", "SANDBOX"):
        return AppMode.TESTBED
    if apex in ("SHADOW_LIVE", "SHADOW"):
        return AppMode.TESTBED
    agent = os.environ.get("IG_AGENT_MODE", "").strip().upper()
    if agent == "SHADOW" or os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return AppMode.TESTBED
    if agent == "LIVE" and os.environ.get("IG_ALLOW_LIVE", "").strip().lower() in _LIVE_ARM_VALUES:
        return AppMode.LIVE
    if agent == "DEMO":
        return AppMode.DEMO
    if apex in ("PRODUCTION", "LIVE", "PROD"):
        allow = os.environ.get("IG_ALLOW_LIVE", "").strip().lower() in _LIVE_ARM_VALUES
        cfg = os.environ.get("IG_AGENT_CONFIG", "").strip().lower()
        if allow and ("live" in cfg or os.environ.get("operating_mode", "").upper() == "LIVE"):
            return AppMode.LIVE
        return AppMode.DEMO
    return None


def resolve_app_mode(*, allow_shim: bool = True) -> AppMode:
    """Resolve APP_MODE from env, optionally falling back to legacy shim."""
    global _MODE, _SHIM_WARNED
    if _MODE is not None:
        return _MODE

    raw = os.environ.get("APP_MODE", "").strip()
    if raw:
        _MODE = parse_app_mode(raw)
        return _MODE

    if allow_shim:
        shim = _legacy_shim_to_app_mode()
        if shim is not None:
            if not _SHIM_WARNED:
                _SHIM_WARNED = True
                log_engine(
                    f"APP_MODE unset — legacy shim mapped to {shim.value} "
                    "(set APP_MODE explicitly; shim will be removed)"
                )
            _MODE = shim
            os.environ["APP_MODE"] = shim.value
            return _MODE
        if os.environ.get("IG_AGENT_PYTEST") == "1":
            _MODE = AppMode.TESTBED
            os.environ["APP_MODE"] = _MODE.value
            return _MODE

    raise ValueError("APP_MODE is required (DEMO, LIVE, or TESTBED)")


def reset_app_mode_for_tests() -> None:
    global _MODE, _SHIM_WARNED
    _MODE = None
    _SHIM_WARNED = False
    try:
        from runtime.session_identity import reset_session_identity_cache_for_tests

        reset_session_identity_cache_for_tests()
    except Exception:
        pass


def live_armed() -> bool:
    return os.environ.get("IG_ALLOW_LIVE", "").strip().lower() in _LIVE_ARM_VALUES


def validate_live_armed(app_mode: AppMode | None = None) -> None:
    """Fail-closed when APP_MODE=LIVE without IG_ALLOW_LIVE arm."""
    mode = app_mode or resolve_app_mode()
    if mode is not AppMode.LIVE:
        return
    if not live_armed():
        msg = "APP_MODE=LIVE rejected — set IG_ALLOW_LIVE=1 to arm live trading"
        raise RuntimeError(msg)


def default_api_port(app_mode: AppMode) -> int:
    return 9199 if app_mode is AppMode.TESTBED else 8080


def default_config_path(app_mode: AppMode) -> str:
    from system.paths import project_root

    root = project_root()
    if app_mode is AppMode.TESTBED:
        return "config/config_v31_testbed.json"
    if app_mode is AppMode.LIVE:
        return "config/config_v31_live_canary.json"
    v31 = root / "config" / "config_v31.json"
    if v31.is_file():
        return "config/config_v31.json"
    return "config/config_v29.json"


def resolve_data_root(app_mode: AppMode) -> str:
    """Return canonical IG_DATA_ROOT path string for the mode."""
    from pathlib import Path

    env = os.environ.get("IG_DATA_ROOT", "").strip()
    if env:
        return str(Path(env).resolve())
    if app_mode is AppMode.TESTBED:
        from system.testbed_firewall import testbed_root

        return str(testbed_root().resolve())
    from system.paths import project_root

    return str((project_root() / "src" / "data" / "v31-production").resolve())


def broker_plane_for(app_mode: AppMode) -> str:
    if app_mode is AppMode.TESTBED:
        return "MOCK"
    if app_mode is AppMode.LIVE:
        return "LIVE"
    return "DEMO"


def apply_app_mode_to_environ() -> AppMode:
    """
    Publish APP_MODE-derived env bundle — must run before apply_runtime_mode_to_environ.
    """
    mode = resolve_app_mode()
    os.environ["APP_MODE"] = mode.value

    port_raw = os.environ.get("IG_API_PORT", "").strip()
    if not port_raw.isdigit():
        os.environ["IG_API_PORT"] = str(default_api_port(mode))

    if not os.environ.get("IG_AGENT_CONFIG", "").strip():
        os.environ["IG_AGENT_CONFIG"] = default_config_path(mode)

    os.environ["IG_BROKER_PLANE"] = broker_plane_for(mode)
    data_root = resolve_data_root(mode)
    os.environ["IG_DATA_ROOT"] = data_root
    # Keep data_dir() and health data_root on the same tree (Tier-0 unify).
    if not os.environ.get("IG_AGENT_DATA_DIR", "").strip():
        os.environ["IG_AGENT_DATA_DIR"] = data_root
    try:
        from pathlib import Path

        from system.paths import bridge_legacy_data_into

        bridge_legacy_data_into(Path(data_root))
    except Exception:
        pass

    if mode is AppMode.TESTBED:
        os.environ["IG_APEX_RUNTIME_MODE"] = "HARDENED_TESTBED"
        os.environ["IG_NODE_PROFILE"] = "testbed"
        os.environ["NODE_ENV"] = "testbed"
        os.environ["IG_AGENT_MODE"] = "SHADOW"
        os.environ["IG_MOCK_FEED"] = "1"
        os.environ.setdefault("IG_ALLOW_MOCK_TRADING", "1")
        os.environ.pop("IG_PRODUCTION_EXECUTION", None)
        log_engine("APP_MODE=TESTBED — sandbox plane (mock REST, isolated data root)")
    elif mode is AppMode.LIVE:
        validate_live_armed(mode)
        os.environ["IG_APEX_RUNTIME_MODE"] = "PRODUCTION"
        os.environ["IG_NODE_PROFILE"] = "production"
        os.environ["NODE_ENV"] = "production"
        os.environ["IG_AGENT_MODE"] = "LIVE"
        os.environ["IG_MOCK_FEED"] = "0"
        os.environ["IG_PRODUCTION_EXECUTION"] = "1"
        os.environ.pop("IG_ALLOW_MOCK_TRADING", None)
        log_engine("APP_MODE=LIVE — production plane, live broker armed")
    else:
        os.environ["IG_APEX_RUNTIME_MODE"] = "PRODUCTION"
        os.environ["IG_NODE_PROFILE"] = "production"
        os.environ["NODE_ENV"] = "production"
        os.environ["IG_AGENT_MODE"] = "DEMO"
        os.environ["IG_MOCK_FEED"] = "0"
        os.environ.pop("IG_PRODUCTION_EXECUTION", None)
        os.environ.pop("IG_ALLOW_MOCK_TRADING", None)
        log_engine("APP_MODE=DEMO — production plane, demo broker")

    return mode


def execution_mode_label(app_mode: AppMode | None = None) -> str:
    """Internal ExecutionMode label derived from APP_MODE."""
    mode = app_mode or resolve_app_mode()
    if mode is AppMode.TESTBED:
        return "TEST"
    if mode is AppMode.LIVE:
        return "LIVE"
    return "DEMO"


def cli_validate_live_or_exit() -> int:
    """CLI helper — exit 2 when LIVE is not armed."""
    try:
        mode = parse_app_mode(os.environ.get("APP_MODE"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if mode is not AppMode.LIVE:
        return 0
    try:
        validate_live_armed(mode)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0
