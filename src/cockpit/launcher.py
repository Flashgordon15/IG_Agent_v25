"""Launch Flight Deck web cockpit — telemetry IPC + WebSocket hub."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from system.engine_log import log_engine

_bridge_started = False
_ipc_ready = threading.Event()
_web_started = False
_lock = threading.Lock()
_COCKPIT_URL = "http://127.0.0.1:8787/"


def cockpit_enabled(cfg: Any | None) -> bool:
    """Return True when Flight Deck auto-launch is configured."""
    return _cockpit_enabled(cfg)


def _cockpit_enabled(cfg: Any | None) -> bool:
    if cfg is None:
        return False
    block = cfg.get("intelligence_layer", {}).get("cockpit", {})
    if not isinstance(block, dict):
        return False
    return bool(block.get("enabled")) and bool(block.get("auto_launch_after_gate4", True))


def _cockpit_cfg(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    block = cfg.get("intelligence_layer", {}).get("cockpit", {})
    return dict(block) if isinstance(block, dict) else {}


def _resolve_epics(cfg: Any | None) -> tuple[str, ...]:
    try:
        from trading.instrument_registry import InstrumentRegistry

        if cfg is None:
            return ()
        reg = InstrumentRegistry(cfg.as_dict())
        epics = [
            str(inst.get("epic") or "").strip()
            for _iid, inst in reg.get_enabled_with_ids()
            if str(inst.get("epic") or "").strip()
        ]
        return tuple(epics) if epics else ()
    except Exception:
        return ()


def _cockpit_port(cfg: Any | None) -> int:
    try:
        from system.node_profile import get_node_profile

        return int(get_node_profile().cockpit_port)
    except Exception:
        pass
    try:
        return int(_cockpit_cfg(cfg).get("web_port", 8787))
    except (TypeError, ValueError):
        return 8787


def _open_cockpit_browser(port: int, *, delay: float = 2.0) -> None:
    """Permanently disabled — Flight Deck must not spawn an external browser."""
    log_engine(
        f"Flight Deck: browser auto-launch disabled — open cockpit manually at "
        f"http://127.0.0.1:{port}/"
    )


def _apex_desktop_mode() -> bool:
    if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
        return True
    if os.environ.get("IG_DESKTOP_FLIGHT_DECK", "").strip() in ("1", "true", "yes"):
        return True
    if os.environ.get("LAUNCHER_DESKTOP", "").strip() in ("1", "true", "yes"):
        return True
    if os.environ.get("IG_DESKTOP_SHELL_ACTIVE", "").strip() in ("1", "true", "yes"):
        return True
    if os.environ.get("IG_APEX_NO_BROWSER", "").strip() == "1":
        return True
    try:
        from system.node_profile import is_shadow_node

        return is_shadow_node()
    except Exception:
        return False


def launch_flight_deck_after_gate4(cfg: Any | None) -> None:
    """Start telemetry bridge + local web cockpit after Gate 4."""
    global _bridge_started, _web_started

    if not _cockpit_enabled(cfg):
        return

    with _lock:
        cockpit_block = _cockpit_cfg(cfg)
        hz = float(cockpit_block.get("telemetry_hz", 2.5))
        port = _cockpit_port(cfg)
        desktop_mode = _apex_desktop_mode()

        if not _bridge_started:
            from cockpit.telemetry_bridge import start_telemetry_bridge

            start_telemetry_bridge(epics=_resolve_epics(cfg) or None, hz=hz)
            _bridge_started = True
            log_engine("Flight Deck telemetry bridge active")

        if os.environ.get("IG_COCKPIT_ISOLATED_EXTERNAL", "").strip() == "1":
            _ipc_ready.set()
            log_engine(
                "Flight Deck: external isolated cockpit owns :8787 — "
                "embedded web server suppressed (read-only SHM consumer)"
            )
            return

        if not _web_started:
            from cockpit.web_server import start_cockpit_web_server

            if start_cockpit_web_server(port=port, hz=hz):
                _web_started = True
                auto_open = bool(cockpit_block.get("auto_open_browser", True))
                if desktop_mode:
                    log_engine(
                        f"Flight Deck web cockpit ready on port {port} "
                        f"(desktop shell embed — browser suppressed)"
                    )
                elif not auto_open:
                    log_engine(
                        f"Flight Deck web cockpit ready on port {port} "
                        f"(browser auto-open disabled)"
                    )
                elif not __import__("os").environ.get("IG_AGENT_PYTEST"):
                    _open_cockpit_browser(port)
                else:
                    log_engine(f"Flight Deck web cockpit ready on port {port}")

        _ipc_ready.set()


def wait_for_cockpit_ipc(*, timeout: float = 180.0) -> bool:
    """Block until Gate 4 has started cockpit IPC (or timeout)."""
    if _ipc_ready.is_set():
        return True
    deadline = time.time() + max(1.0, float(timeout))
    last_log = 0.0
    while time.time() < deadline:
        remaining = deadline - time.time()
        if _ipc_ready.wait(timeout=min(0.25, max(0.05, remaining))):
            return True
        now = time.time()
        if now - last_log >= 15.0:
            log_engine("Flight Deck: waiting for Gate 4 cockpit IPC…")
            last_log = now
    return _ipc_ready.is_set()


def run_flight_deck_on_main_thread(cfg: Any | None, *, ipc_timeout: float = 180.0) -> None:
    """Legacy entry — web cockpit runs async; block until process shutdown."""
    if not _cockpit_enabled(cfg):
        return
    wait_for_cockpit_ipc(timeout=ipc_timeout)
    log_engine("Flight Deck web mode — cockpit served in background thread")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def stop_flight_deck() -> None:
    global _bridge_started, _web_started
    with _lock:
        _bridge_started = False
        _web_started = False
        _ipc_ready.clear()

    try:
        from cockpit.web_server import stop_cockpit_web_server

        stop_cockpit_web_server()
    except Exception:
        pass

    try:
        from cockpit.telemetry_bridge import stop_telemetry_bridge

        stop_telemetry_bridge()
    except Exception:
        pass


def reset_flight_deck_for_tests() -> None:
    stop_flight_deck()
    try:
        from cockpit.telemetry_bridge import reset_telemetry_bridge_for_tests
        from cockpit.emergency import reset_emergency_override_for_tests
        from cockpit.web_server import reset_cockpit_web_for_tests

        reset_cockpit_web_for_tests()
        reset_telemetry_bridge_for_tests()
        reset_emergency_override_for_tests()
    except Exception:
        pass
