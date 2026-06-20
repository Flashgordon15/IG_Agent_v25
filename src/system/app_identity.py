"""Single source of truth for IG Agent version and runtime identity."""

from __future__ import annotations

APP_VERSION = "30.0.0"
APP_VERSION_LABEL = "v30.0"
APP_DISPLAY_NAME = "IG Agent Apex"
APP_SHORT_NAME = "IG Agent Apex"

# Instance lock — v30 shadow primary; legacy files cleared on acquire/release.
INSTANCE_LOCK_FILE = ".ig_agent_v30_shadow.lock"
LEGACY_LOCK_FILES: tuple[str, ...] = (
    ".ig_agent_v29.lock",
    ".ig_agent_v25.lock",
    ".ig_agent_v24.lock",
)

# launchd bundle IDs (historical v25 prefix — stable across macOS installs).
LAUNCHD_WATCHDOG_LABEL = "com.igagent.v25.watchdog"
LAUNCHD_CAFF_LABEL = "com.igagent.v25.caffeinate"
LAUNCHD_AGENT_LABEL = "com.igagent.v25"
