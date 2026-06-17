"""
Project-root ``.env`` loader — zero-friction headless IG credentials.

Loads ``IG_USERNAME``, ``IG_PASSWORD``, ``IG_API_KEY``, ``ACCOUNT_TYPE``, and
optional ``IG_ACCOUNT_ID`` into ``os.environ`` before any boot gate runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ENV_LOADED = False


def reset_dotenv_state() -> None:
    """Test helper — allow reloading ``.env`` from disk."""
    global _ENV_LOADED
    _ENV_LOADED = False

_ENV_TO_CRED: dict[str, str] = {
    "IG_USERNAME": "ig_username",
    "IG_PASSWORD": "ig_password",
    "IG_API_KEY": "ig_api_key",
    "ACCOUNT_TYPE": "ig_account_type",
    "IG_ACCOUNT_ID": "ig_account_id",
}

# Legacy headless bridge — mapped after dotenv load
_LEGACY_ENV_PASSWORD_KEYS = ("IG_PASSWORD", "AGENT_LAUNCH_PASS")


def dotenv_path() -> Path:
    from system.paths import project_root

    return project_root() / ".env"


def load_dotenv(*, override: bool = False) -> bool:
    """Load project-root ``.env`` into ``os.environ``. Idempotent unless ``override``."""
    global _ENV_LOADED
    path = dotenv_path()
    if not path.is_file():
        return False
    if _ENV_LOADED and not override:
        return True

    loaded = False
    try:
        from dotenv import load_dotenv as _dotenv_load

        loaded = bool(_dotenv_load(path, override=override))
    except ImportError:
        loaded = _parse_dotenv_manual(path, override=override)

    _ENV_LOADED = True
    return loaded


def _parse_dotenv_manual(path: Path, *, override: bool) -> bool:
    """Minimal dotenv parser when python-dotenv is unavailable."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key:
            continue
        if not override and os.environ.get(key, "").strip():
            continue
        os.environ[key] = value
    return True


def get_env_credential_fields() -> dict[str, str]:
    """Return canonical ig_* credential fields sourced from environment."""
    out: dict[str, str] = {}
    for env_key, cred_key in _ENV_TO_CRED.items():
        value = os.environ.get(env_key, "").strip()
        if value:
            out[cred_key] = value
    return out


def env_credentials_complete() -> bool:
    """True when all required credential env keys are present."""
    fields = get_env_credential_fields()
    required = (
        "ig_username",
        "ig_password",
        "ig_api_key",
        "ig_account_type",
        "ig_account_id",
    )
    return all(fields.get(key, "").strip() for key in required)


def apply_env_to_credentials(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay ``.env`` credential keys onto a credentials JSON dict."""
    out = dict(raw)
    for cred_key, value in get_env_credential_fields().items():
        out[cred_key] = value

    # Legacy AGENT_LAUNCH_PASS only when password still empty / encrypted
    password = str(out.get("ig_password") or out.get("password") or "").strip()
    if not password or password.startswith("enc:"):
        for legacy_key in _LEGACY_ENV_PASSWORD_KEYS:
            legacy = os.environ.get(legacy_key, "").strip()
            if legacy:
                out["ig_password"] = legacy
                if not str(out.get("password") or "").strip():
                    out["password"] = legacy
                break
    return out


def prepare_boot_env() -> None:
    """
    Early boot hook — load ``.env`` and propagate dashboard admin auth.

    Safe to call multiple times; never logs secret values.
    """
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return

    _from_launcher = os.environ.get("IG_AGENT_FROM_LAUNCHER") == "1" or (
        os.environ.get("IG_AGENT_DESKTOP_LAUNCH") == "1"
    )
    load_dotenv(override=_from_launcher)

    ig_password = os.environ.get("IG_PASSWORD", "").strip()
    if not ig_password:
        ig_password = os.environ.get("AGENT_LAUNCH_PASS", "").strip()

    if ig_password and not os.environ.get("ADMIN_PASSWORD", "").strip():
        os.environ["ADMIN_PASSWORD"] = ig_password

    if ig_password and not os.environ.get("IG_PASSWORD", "").strip():
        os.environ["IG_PASSWORD"] = ig_password
