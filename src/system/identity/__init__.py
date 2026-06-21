"""Unified runtime identity — port, lock, and script pointer resolution."""

from system.identity.app_identity import (
    APP_DISPLAY_NAME,
    APP_SHORT_NAME,
    APP_VERSION,
    APP_VERSION_LABEL,
    INSTANCE_LOCK_FILE,
    LAUNCHD_AGENT_LABEL,
    LAUNCHD_CAFF_LABEL,
    LAUNCHD_WATCHDOG_LABEL,
    LEGACY_LOCK_FILES,
    RuntimeIdentity,
)

__all__ = [
    "APP_DISPLAY_NAME",
    "APP_SHORT_NAME",
    "APP_VERSION",
    "APP_VERSION_LABEL",
    "INSTANCE_LOCK_FILE",
    "LAUNCHD_AGENT_LABEL",
    "LAUNCHD_CAFF_LABEL",
    "LAUNCHD_WATCHDOG_LABEL",
    "LEGACY_LOCK_FILES",
    "RuntimeIdentity",
]
