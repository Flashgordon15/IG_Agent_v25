"""
v30 Apex — runtime multi-tenant node profile (production vs shadow).

Resolved from ``NODE_ENV`` (or ``IG_NODE_PROFILE``) at process boot. v30 monolith
uses the isolated ``v30-production`` namespace; legacy v25/v29 ``src/data`` is never
touched when ``APP_VERSION`` is 30.x.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from system.app_identity import APP_VERSION
from system.identity.app_identity import RuntimeIdentity
from system.paths import analytics_dir, data_dir, logs_dir, triage_db_path

NodeKind = Literal["production", "shadow", "testbed"]

_PROFILE: "NodeProfile | None" = None


@dataclass(frozen=True)
class NodeProfile:
    kind: NodeKind
    api_port: int
    cockpit_port: int
    runtime_state_file: Path
    engine_log_file: Path
    analytics_db: Path
    triage_db: Path
    instance_lock_file: str
    learning_db: Path
    ipc_socket_name: str
    version_label: str

    @property
    def is_testbed(self) -> bool:
        return self.kind == "testbed"

    @property
    def is_shadow(self) -> bool:
        return self.kind == "shadow"

    @property
    def is_production(self) -> bool:
        return self.kind == "production"

    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}/"

    @property
    def cockpit_url(self) -> str:
        return f"http://127.0.0.1:{self.cockpit_port}/"


def _is_apex_desktop_shell() -> bool:
    return (
        os.environ.get("IG_APEX_DESKTOP", "").strip() == "1"
        or os.environ.get("IG_AGENT_DESKTOP_LAUNCH", "").strip() == "1"
    )


def _is_macos_launcher_production() -> bool:
    """IGAgent.app on :8080 — production DEMO plane, not isolated shadow sandbox."""
    return os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1" or (
        os.environ.get("LAUNCHER_DESKTOP", "").strip() == "1"
    )


def _resolve_kind() -> NodeKind:
    try:
        from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

        if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
            return "testbed"
    except Exception:
        pass
    if _is_macos_launcher_production():
        return "production"
    if _is_apex_desktop_shell():
        return "shadow"
    raw = (
        os.environ.get("IG_NODE_PROFILE", "").strip()
        or os.environ.get("NODE_ENV", "").strip()
        or "production"
    ).lower()
    if raw in ("shadow", "v30", "sandbox", "development"):
        return "shadow"
    return "production"


def _build_profile(kind: NodeKind) -> NodeProfile:
    if kind == "testbed":
        from system.testbed_firewall import testbed_ledger_path, testbed_state_path

        ledger = testbed_ledger_path()
        state = testbed_state_path()
        api_port = int(os.environ.get("IG_API_PORT", "9199"))
        return NodeProfile(
            kind="testbed",
            api_port=api_port,
            cockpit_port=9299,
            runtime_state_file=state,
            engine_log_file=logs_dir() / "testbed.log",
            analytics_db=ledger,
            triage_db=ledger,
            instance_lock_file=RuntimeIdentity.lock_basename(api_port),
            learning_db=ledger,
            ipc_socket_name="testbed_ipc.sock",
            version_label="30.0.0-testbed",
        )
    analytics = analytics_dir()
    triage = triage_db_path()
    ver = str(APP_VERSION)
    if ver.startswith("31."):
        version_label = ver
        v30_monolith = True
    elif ver.startswith("30."):
        version_label = "30.0.0"
        v30_monolith = True
    else:
        version_label = "v29.1.0"
        v30_monolith = False
    if kind == "shadow":
        api_port = int(os.environ.get("IG_API_PORT", "9090"))
        return NodeProfile(
            kind="shadow",
            api_port=api_port,
            cockpit_port=9191,
            runtime_state_file=data_dir() / "runtime_state_shadow.json",
            engine_log_file=logs_dir() / "shadow_v30.log",
            analytics_db=triage,
            triage_db=triage,
            instance_lock_file=RuntimeIdentity.lock_basename(api_port),
            learning_db=data_dir() / "learning_db_shadow.sqlite3",
            ipc_socket_name="apex_ipc.sock",
            version_label=version_label,
        )
    api_port = int(os.environ.get("IG_API_PORT", "8080"))
    return NodeProfile(
        kind="production",
        api_port=api_port,
        cockpit_port=8787,
        runtime_state_file=data_dir() / "runtime_state.json",
        engine_log_file=logs_dir() / "production.log",
        analytics_db=triage if v30_monolith else analytics / "production.db",
        triage_db=triage if v30_monolith else analytics / "production.db",
        instance_lock_file=RuntimeIdentity.lock_basename(api_port),
        learning_db=data_dir() / "learning_db.sqlite3",
        ipc_socket_name="apex_ipc.sock",
        version_label=version_label,
    )


def get_node_profile(*, reload: bool = False) -> NodeProfile:
    global _PROFILE
    if _PROFILE is None or reload:
        _PROFILE = _build_profile(_resolve_kind())
    return _PROFILE


def is_shadow_node() -> bool:
    return get_node_profile().is_shadow


def is_production_node() -> bool:
    return get_node_profile().is_production


def apply_node_profile_to_environ() -> NodeProfile:
    """Publish profile paths into os.environ for legacy module reads."""
    profile = get_node_profile()
    os.environ["IG_NODE_PROFILE"] = profile.kind
    os.environ["IG_API_PORT"] = str(profile.api_port)
    os.environ["IG_COCKPIT_PORT"] = str(profile.cockpit_port)
    os.environ["IG_RUNTIME_STATE_FILE"] = str(profile.runtime_state_file)
    os.environ["IG_ENGINE_LOG_FILE"] = str(profile.engine_log_file)
    os.environ["IG_ANALYTICS_DB"] = str(profile.analytics_db)
    os.environ["IG_TRIAGE_DB"] = str(profile.triage_db)
    os.environ["IG_LEARNING_DB"] = str(profile.learning_db)
    os.environ["IG_APEX_IPC_SOCKET"] = profile.ipc_socket_name
    RuntimeIdentity.export_pointer_for_scripts()
    if profile.is_shadow:
        os.environ["IG_AGENT_SHADOW_DESK"] = "1"
        os.environ.setdefault("IG_AGENT_SKIP_ORPHAN_KILL", "1")
        os.environ.setdefault("IG_APEX_PROTECT_PRODUCTION_PORTS", "1")
    return profile


def reset_node_profile_for_tests() -> None:
    global _PROFILE
    _PROFILE = None
