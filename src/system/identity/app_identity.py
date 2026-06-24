"""Single source of truth for IG Agent version and runtime identity."""

from __future__ import annotations

import os
from pathlib import Path

APP_VERSION = "30.0.0"
APP_VERSION_LABEL = "v30.0"
APP_DISPLAY_NAME = "IG Agent Apex"
APP_SHORT_NAME = "IG Agent Apex"

# Legacy lock basenames cleared on acquire (idempotent migration).
LEGACY_LOCK_FILES: tuple[str, ...] = (
    ".ig_agent_v29.lock",
    ".ig_agent_v25.lock",
    ".ig_agent_v24.lock",
    ".ig_agent_v30_shadow.lock",
    ".ig_agent_testbed.lock",
)

# Canonical pattern — actual path includes resolved port suffix.
INSTANCE_LOCK_FILE = ".ig_agent_v30_port_{port}.lock"

LAUNCHD_WATCHDOG_LABEL = "com.igagent.v25.watchdog"
LAUNCHD_CAFF_LABEL = "com.igagent.v25.caffeinate"
LAUNCHD_AGENT_LABEL = "com.igagent.v25"

_GLOBAL_POINTER_DIR = Path.home() / ".ig_agent_global"
_ACTIVE_LOCK_POINTER = _GLOBAL_POINTER_DIR / "active_lock_pointer"


class RuntimeIdentity:
    """Unified port + lock resolution for Python, shell, and launchd supervision."""

    @staticmethod
    def resolve_mode() -> str:
        """Return HARDENED_TESTBED, SHADOW, DEMO, or PRODUCTION."""
        try:
            from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

            if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
                return "HARDENED_TESTBED"
        except Exception:
            pass
        if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
            return "SHADOW"
        raw = (
            os.environ.get("IG_NODE_PROFILE", "").strip()
            or os.environ.get("NODE_ENV", "").strip()
            or "production"
        ).lower()
        if raw in ("shadow", "v30", "sandbox", "development"):
            return "SHADOW"
        if raw in ("demo",):
            return "DEMO"
        return "PRODUCTION"

    @staticmethod
    def resolve_api_port() -> int:
        """
        Port routing (fail-closed defaults):
          HARDENED_TESTBED → 9199
          SHADOW / DEMO    → 9090
          PRODUCTION       → 8080
        ``IG_API_PORT`` overrides when set to a positive integer.
        """
        env_raw = os.environ.get("IG_API_PORT", "").strip()
        if env_raw.isdigit():
            port = int(env_raw)
            if port > 0:
                return port

        mode = RuntimeIdentity.resolve_mode()
        if mode == "HARDENED_TESTBED":
            return 9199
        if mode in ("SHADOW", "DEMO"):
            return 9090
        return 8080

    @staticmethod
    def lock_basename(port: int | None = None) -> str:
        bind_port = int(port if port is not None else RuntimeIdentity.resolve_api_port())
        return f".ig_agent_v30_port_{bind_port}.lock"

    @staticmethod
    def get_lock_path(port: int | None = None) -> Path:
        """Port-scoped instance lock under the active data directory."""
        from system.paths import data_dir

        return data_dir() / RuntimeIdentity.lock_basename(port)

    @staticmethod
    def export_pointer_for_scripts() -> Path:
        """
        Write absolute lock path for external scripts (watchdog.sh, launchd).

        Idempotent — safe to call on every boot.
        """
        if os.environ.get("IG_PARALLEL_V31_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
            bind_port = int(RuntimeIdentity.resolve_api_port())
            os.environ["IG_API_PORT"] = str(bind_port)
            os.environ["IG_INSTANCE_LOCK_FILE"] = RuntimeIdentity.lock_basename(bind_port)
            return _ACTIVE_LOCK_POINTER
        lock_path = RuntimeIdentity.get_lock_path()
        _GLOBAL_POINTER_DIR.mkdir(parents=True, exist_ok=True)
        _ACTIVE_LOCK_POINTER.write_text(f"{lock_path.resolve()}\n", encoding="utf-8")
        os.environ["IG_ACTIVE_LOCK_POINTER"] = str(_ACTIVE_LOCK_POINTER)
        os.environ["IG_INSTANCE_LOCK_FILE"] = lock_path.name
        os.environ["IG_API_PORT"] = str(RuntimeIdentity.resolve_api_port())
        return _ACTIVE_LOCK_POINTER

    @staticmethod
    def read_active_lock_pointer() -> Path | None:
        """Resolve lock path from exported pointer file (shell / watchdog)."""
        pointer = os.environ.get("IG_ACTIVE_LOCK_POINTER", "").strip()
        path = Path(pointer) if pointer else _ACTIVE_LOCK_POINTER
        try:
            if path.is_file():
                raw = path.read_text(encoding="utf-8").strip()
                if raw:
                    return Path(raw)
        except OSError:
            pass
        return None

    @staticmethod
    def legacy_lock_paths() -> list[Path]:
        from system.paths import data_dir

        root = data_dir()
        paths = [root / name for name in LEGACY_LOCK_FILES]
        # Port-scoped locks from prior sessions on other profiles.
        try:
            for entry in root.glob(".ig_agent_v30_port_*.lock"):
                paths.append(entry)
        except OSError:
            pass
        return paths
