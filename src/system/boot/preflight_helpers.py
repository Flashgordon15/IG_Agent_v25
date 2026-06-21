"""
Lightweight preflight helpers ported from ``main.py`` (Gate 1 only).

All non-stdlib imports are deferred to function scope so Gate 1 does not pull
trading, ML, or database modules at import time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024
_LOG_KEEP_BACKUPS = 3
# Never use "localhost" — macOS mDNS can stall connect() for tens of seconds.
_LOOPBACK_IPV4 = "127.0.0.1"
_DEFAULT_API_PORT = 8080


def resolve_api_port() -> int:
    """Profile-assigned API bind port (9090 shadow, 8080 production)."""
    try:
        from system.node_profile import get_node_profile

        return int(get_node_profile().api_port)
    except Exception:
        import os

        raw = os.environ.get("IG_API_PORT", str(_DEFAULT_API_PORT)).strip()
        try:
            return int(raw)
        except ValueError:
            return _DEFAULT_API_PORT


def config_path() -> Path:
    from system.config_loader import _primary_config_path

    return _primary_config_path()


def load_raw_config_dict() -> dict[str, Any]:
    """Load fully merged config (v29 → v25 $extends chain)."""
    from system.config_loader import ConfigLoader

    return ConfigLoader(config_path()).load_config(validate=False).as_dict()


def merge_credentials_for_validation(data: dict[str, Any]) -> dict[str, Any]:
    from system.credentials_loader import try_load_credentials

    merged = dict(data)
    status = try_load_credentials()
    if status.credentials is not None:
        c = status.credentials
        merged.update(
            {
                "ig_username": c.ig_username,
                "ig_password": c.ig_password,
                "ig_api_key": c.ig_api_key,
                "ig_account_id": c.ig_account_id,
                "account_id": c.ig_account_id,
            }
        )
    return merged


def rotate_oversized_logs() -> None:
    """Rotate shell-written logs that exceed the size cap (from main._rotate_oversized_logs)."""
    from system.paths import logs_dir

    log_dir = logs_dir()
    for log_path in log_dir.glob("*.log"):
        try:
            if log_path.stat().st_size <= _LOG_ROTATE_MAX_BYTES:
                continue
            for i in range(_LOG_KEEP_BACKUPS - 1, 0, -1):
                src = Path(f"{log_path}.{i}")
                dst = Path(f"{log_path}.{i + 1}")
                if src.exists():
                    src.rename(dst)
            log_path.rename(Path(f"{log_path}.1"))
            log_path.touch()
        except Exception as exc:
            from system.guard.runtime_guard import log_guarded_exception

            log_guarded_exception("preflight_helpers", exc)


def check_port_available(port: int | None = None) -> bool:
    """Return True when 127.0.0.1:port is free to bind (no localhost DNS).

    When *port* is omitted, uses the active node profile API port exclusively
    (9090 shadow / 8080 production). Shadow never probes production :8080.
    """
    import socket

    bind_port = resolve_api_port() if port is None else port
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((_LOOPBACK_IPV4, bind_port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def production_port_in_use() -> bool:
    """True when production :8080 is bound — informational only for shadow boot."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return probe.connect_ex((_LOOPBACK_IPV4, 8080)) == 0
    finally:
        probe.close()


def is_benign_startup_lock_failure(message: str) -> bool:
    txt = str(message or "").strip().lower()
    if not txt:
        return False
    markers = (
        "another ig agent instance is running",
        "already running",
        "duplicate",
    )
    return any(marker in txt for marker in markers)


def load_validated_config() -> Any:
    from system.config import Config
    from system.config_validator import apply_config_defaults

    raw = load_raw_config_dict()
    merged = apply_config_defaults(raw)
    return Config(_data=merged)
