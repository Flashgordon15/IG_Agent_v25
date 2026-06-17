"""
Headless launch credential bridge — delegates to project-root ``.env``.

Interactive terminal password prompts have been removed. Credentials are loaded
programmatically via :mod:`system.env_loader` on every boot.
"""

from __future__ import annotations

import os
from typing import Any

from system.env_loader import apply_env_to_credentials, prepare_boot_env


def is_headless_launch() -> bool:
    """Always headless for credential purposes — no stdin password prompts."""
    return True


def launch_password_from_env() -> str | None:
    value = os.environ.get("IG_PASSWORD", "").strip()
    if value:
        return value
    return os.environ.get("AGENT_LAUNCH_PASS", "").strip() or None


def resolve_launch_password(*, allow_prompt: bool = False) -> str | None:
    """Resolve IG password from environment only (never prompts)."""
    _ = allow_prompt
    prepare_boot_env()
    return launch_password_from_env()


def apply_launch_password_to_credentials(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay credentials from ``.env`` / legacy env keys."""
    prepare_boot_env()
    return apply_env_to_credentials(raw)


def prepare_launch_auth() -> None:
    """Early boot hook — load ``.env`` credentials before Gate 1."""
    prepare_boot_env()
