"""Single source of truth for IG Agent version and runtime identity."""

from __future__ import annotations

APP_VERSION = "29.1.0"
APP_VERSION_LABEL = "v29.1"
APP_DISPLAY_NAME = "IG Agent v29"
APP_SHORT_NAME = "IG Agent"

# Instance lock — v29 primary; legacy files cleared on acquire/release.
INSTANCE_LOCK_FILE = ".ig_agent_v29.lock"
LEGACY_LOCK_FILES: tuple[str, ...] = (".ig_agent_v25.lock", ".ig_agent_v24.lock")

# launchd bundle IDs (historical v25 prefix — stable across macOS installs).
LAUNCHD_WATCHDOG_LABEL = "com.igagent.v25.watchdog"
LAUNCHD_CAFF_LABEL = "com.igagent.v25.caffeinate"
LAUNCHD_AGENT_LABEL = "com.igagent.v25"
