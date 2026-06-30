"""Supervisor + cockpit launch contract validation (read-only)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_GUI = ROOT / "macos" / "launcher" / "agent_gui.sh"
COCKPIT_PROVIDER = ROOT / "gui" / "ig_cockpit" / "src" / "hooks" / "CockpitProvider.tsx"
COCKPIT_API = ROOT / "gui" / "ig_cockpit" / "src" / "lib" / "api.ts"


def test_agent_gui_launch_order():
    """Cockpit launch order: release app → debug app → release binary → debug binary → tauri:dev → browser."""
    text = AGENT_GUI.read_text(encoding="utf-8")
    start = text.index('if [[ -d "${APP_RELEASE}" ]]; then')
    segment = text[start:]
    chain = [
        "APP_RELEASE",
        "APP_DEBUG",
        "SUP_RELEASE",
        "SUP_DEBUG",
        "tauri:dev",
        "open_browser_fallback",
    ]
    positions = [segment.index(m) for m in chain]
    assert positions == sorted(positions), f"Launch chain order wrong: {chain}"


def test_splash_waits_for_gui_and_ws():
    text = COCKPIT_PROVIDER.read_text(encoding="utf-8")
    assert "SPLASH_MAX_MS = 45_000" in text
    assert "isGuiFullyReady" in text
    assert 'wsState === "connected"' in text or "wsConnected" in text
    assert "openBrowserFallback" in text or "browser fallback" in text.lower()


def test_agent_control_fire_and_forget():
    text = COCKPIT_API.read_text(encoding="utf-8")
    assert re.search(r"export function pauseTrading\(\): void", text)
    assert re.search(r"void postJson\(", text)
    assert "fire-and-forget" in text.lower() or "Non-blocking" in text


def test_rest_debounce_350ms():
    text = COCKPIT_PROVIDER.read_text(encoding="utf-8")
    assert "REST_DEBOUNCE_MS = 350" in text
