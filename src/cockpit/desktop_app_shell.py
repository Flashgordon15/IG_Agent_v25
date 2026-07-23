#!/usr/bin/env python3
"""
Iron Cage Flight Deck — unified native desktop shell (pywebview / WKWebView).

Premium 1440×900 frameless window with 9-stage splash, embedded terminal stream,
and fail-safe anti-zombie teardown on exit.

Launch:
  PYTHONPATH=src .venv/bin/python3 -m cockpit.desktop_app_shell

Smoke:
  PYTHONPATH=src .venv/bin/python3 -m cockpit.desktop_app_shell --smoke-test
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cockpit.desktop_process_guard import (
    audit_and_purge_bound_ports,
    execute_anti_zombie_shutdown,
    execute_sequential_desktop_teardown,
    register_child_pid,
    register_child_process,
)
from cockpit.desktop_splash_assets import (
    LAUNCHER_STAGES,
    build_splash_html,
    launcher_stage_index,
    orchestrator_segment_states,
)
from system.engine_log import log_engine

WINDOW_TITLE = "IG Trading Desk v34"
WINDOW_WIDTH_DEFAULT = 1920
WINDOW_HEIGHT_DEFAULT = 1080
WINDOW_MIN_WIDTH = 1280
WINDOW_MIN_HEIGHT = 720
BACKGROUND = "#0D0E12"
COCKPIT_PORT = 8787
API_PORT = 8080
SB_API_PORT = 8081
TERMINAL_UI_PORT = 3000
TERMINAL_UI_URL_PREFIX = f"http://localhost:{TERMINAL_UI_PORT}"
DEFAULT_TRADING_DESK_URL = f"{TERMINAL_UI_URL_PREFIX}/boot"
POLL_MS = 450
LOG_POLL_MS = 350
_COCKPIT_CLEARANCE_HOLD_SEC = 3.0
_HEALTH_RETRY_MAX = 5
_HEALTH_RETRY_MS = 1000.0
_HANDOFF_BLOCK_WARN_SEC = 6.0
_HANDOFF_FORCE_SEC = 18.0
_STAGE5_TOKENS = frozenset({"SUCCESS", "HEALTHY", "WARMING_HEALTHY"})

_ROOT = Path(__file__).resolve().parents[2]
_RUNNING = threading.Event()
_WINDOW: Any = None
_LAUNCHER_PROC: subprocess.Popen[Any] | None = None
_COCKPIT_EMBEDDED = False
_EVAL_LOCK = threading.Lock()
_CLEARANCE_LOCK = threading.Lock()
_CLEARANCE_STATE: dict[str, Any] = {
    "cleared": False,
    "stage5_since": None,
    "synthetic_since": None,
}
_CLEARANCE_MONITOR_STARTED = False
_SYNTH_HYDRATION_LOGGED = False
_HANDOFF_ELIGIBLE_SINCE: float | None = None
_HANDOFF_BLOCK_LOGGED = False

_LAUNCHER_COCKPIT_STAGES = frozenset(
    {"gui", "ready", "verify", "warmup", "post_ready", "g5", "agent_boot"}
)

_FORCE_COCKPIT_LIVE_JS = """
(function(){
  try {
    document.body.classList.add('cockpit-live');
    var overlay = document.getElementById('boot-splash-overlay');
    var shell = document.getElementById('cockpit-main-shell');
    var frame = document.getElementById('cockpit-live-frame');
    var strip = document.getElementById('avionics-command-strip');
    if (overlay) {
      overlay.classList.remove('active');
      overlay.classList.add('fade-out', 'cleared');
      overlay.setAttribute('aria-hidden', 'true');
      overlay.setAttribute('aria-modal', 'false');
    }
    if (shell) shell.classList.add('cockpit-ready');
    if (frame) frame.classList.add('live-split');
    if (strip) { strip.hidden = false; strip.removeAttribute('hidden'); }
    if (typeof window.__forceCockpitLive === 'function') {
      window.__forceCockpitLive();
    } else if (typeof window.transitionToLiveCockpit === 'function') {
      window.transitionToLiveCockpit();
    }
  } catch (e) {
    console.error('[DesktopShell] native clearance', e);
  }
})();
""".strip()


def _project_root() -> Path:
    env = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    return _ROOT


def _launcher_status_path() -> Path:
    return _project_root() / "logs" / "launcher_status.json"


def _log_paths() -> tuple[Path, ...]:
    root = _project_root()
    return tuple(
        p
        for p in (
            root / "logs" / "igagent_launcher.log",
            root / "logs" / "agent_start.log",
            root / "logs" / "agent_kill.log",
            root / "logs" / "cockpit_launch.log",
        )
        if p.is_file()
    )


def _fetch_json(url: str, *, timeout: float = 1.8) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _url_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=1.5):
            return True
    except Exception:
        try:
            with urllib.request.urlopen(url, timeout=1.5):
                return True
        except Exception:
            return False


def _js_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _evaluate(js: str) -> None:
    import webview  # noqa: WPS433

    global _WINDOW
    with _EVAL_LOCK:
        try:
            win = _WINDOW or (webview.windows[0] if webview.windows else None)
            if win is not None:
                win.evaluate_js(js)
        except Exception as exc:
            log_engine(f"DesktopShell evaluate_js: {type(exc).__name__}: {exc}")


def _append_terminal(line: str, *, level: str = "") -> None:
    lvl = level or ("err" if "error" in line.lower() or "fail" in line.lower() else "")
    if "warn" in line.lower():
        lvl = lvl or "warn"
    if "ok" in line.lower() or "complete" in line.lower() or "ready" in line.lower():
        lvl = lvl or "ok"
    _evaluate(
        f"window.__desktopShell && window.__desktopShell.appendTerminal('{_js_escape(line)}', '{lvl}');"
    )


def _read_launcher_status() -> dict[str, Any] | None:
    path = _launcher_status_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _cache_bust_cockpit_url(url: str) -> str:
    """Monotonic cache-bust query — defeats WKWebView stale script locks on cold boot."""
    mono = int(time.time() * 1000)
    nonce = os.urandom(4).hex()
    base = str(url or "").split("?")[0].rstrip("/") + "/"
    return f"{base}?v={mono}&_cb={nonce}&t={mono}"


def _is_trading_desk_mode() -> bool:
    return os.environ.get("IG_TRADING_DESK_NATIVE", "").strip().lower() in ("1", "true", "yes")


def _is_flight_deck_embed_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or "").strip())
        if parsed.port:
            return int(parsed.port) == int(COCKPIT_PORT)
        return False
    except (TypeError, ValueError):
        return False


def _should_use_load_url(url: str) -> bool:
    return not _is_flight_deck_embed_url(url)


def _is_trading_desk_embed_url(url: str) -> bool:
    return _should_use_load_url(url)


def _resolve_embed_cockpit_url() -> str:
    """Quantum Terminal (:3000) boot splash or legacy Flight Deck (:8787)."""
    if _is_trading_desk_mode():
        explicit = (os.environ.get("IG_COCKPIT_URL") or "").strip()
        if explicit and not _is_flight_deck_embed_url(explicit):
            try:
                from urllib.parse import urlparse

                port = urlparse(explicit).port
                if port is None or int(port) != int(API_PORT):
                    return explicit
            except (TypeError, ValueError):
                return explicit
        return DEFAULT_TRADING_DESK_URL
    return f"http://127.0.0.1:{COCKPIT_PORT}/"


def _read_cache_busted_cockpit_html(*, base_url: str) -> str | None:
    """
    Read cockpit-web/index.html from disk and inject per-boot cache-bust tokens
    into stylesheet + app.js references before WKWebView hydration.
    """
    index_path = _project_root() / "cockpit-web" / "index.html"
    if not index_path.is_file():
        return None
    try:
        html = index_path.read_text(encoding="utf-8")
        mono = int(time.time() * 1000)
        nonce = os.urandom(4).hex()
        html = html.replace(
            "window.__COCKPIT_BUILD__ = Date.now();",
            (
                f"window.__COCKPIT_BUILD__ = {mono}; "
                f"window.__COCKPIT_CACHE_NONCE__ = '{nonce}';"
            ),
        )
        html = html.replace(
            '"/static/styles.css?v=" + window.__COCKPIT_BUILD__',
            (
                '"/static/styles.css?v=" + window.__COCKPIT_BUILD__ '
                '+ "&_cb=" + window.__COCKPIT_CACHE_NONCE__'
            ),
        )
        html = html.replace(
            '"/static/app.js?v=" + (window.__COCKPIT_BUILD__ || Date.now())',
            (
                '"/static/app.js?v=" + window.__COCKPIT_BUILD__ '
                '+ "&t=" + window.__COCKPIT_BUILD__ '
                '+ "&_cb=" + window.__COCKPIT_CACHE_NONCE__'
            ),
        )
        if base_url:
            _ = base_url  # base_uri supplied separately to load_html
        return html
    except OSError as exc:
        log_engine(f"DesktopShell cache-bust html read: {type(exc).__name__}: {exc}")
        return None


def _stage5_launch_ready(
    orch: dict[str, Any] | None,
    status: dict[str, Any] | None,
) -> bool:
    if isinstance(orch, dict):
        tokens = orch.get("stage_tokens") or {}
        if isinstance(tokens, dict):
            token = str(tokens.get("STAGE_5_LAUNCH") or "").upper()
            if token in _STAGE5_TOKENS:
                return True
    if isinstance(status, dict):
        stage = str(status.get("stage") or "").lower()
        step = int(status.get("step") or 0)
        detail = str(status.get("detail") or "").lower()
        if stage in ("gui", "ready", "warmup", "post_ready") and step >= 5:
            return True
        if stage in ("g5", "agent_boot") and step >= 5:
            return True
        if stage in ("post_ready", "warmup", "verify", "gui", "ready"):
            return True
        if "g5 complete" in detail or "stage 5 complete" in detail:
            return True
        if "stage 5" in detail or "g5" in detail or "stage_5" in detail:
            return True
    return False


def _synthetic_hydration_active(diag: dict[str, Any] | None) -> bool:
    if not isinstance(diag, dict):
        return False
    if diag.get("synthetic_hydration_active") is True:
        return True
    recovery = diag.get("transport_recovery")
    return isinstance(recovery, dict) and recovery.get("synthetic_hydration_active") is True


def _force_cockpit_live_layout_native() -> None:
    """Atomic native override — shatter splash deadlock from Python layer."""
    _evaluate(_FORCE_COCKPIT_LIVE_JS)


def _tick_cockpit_clearance_override(
    *,
    orch: dict[str, Any] | None,
    diag: dict[str, Any] | None,
    status: dict[str, Any] | None,
) -> bool:
    """Return True when native clearance was triggered this tick."""
    with _CLEARANCE_LOCK:
        if _CLEARANCE_STATE.get("cleared"):
            return False
        now = time.time()
        if _stage5_launch_ready(orch, status):
            if _CLEARANCE_STATE.get("stage5_since") is None:
                _CLEARANCE_STATE["stage5_since"] = now
        if _synthetic_hydration_active(diag):
            if _CLEARANCE_STATE.get("synthetic_since") is None:
                _CLEARANCE_STATE["synthetic_since"] = now

        stage5_since = _CLEARANCE_STATE.get("stage5_since")
        synthetic_since = _CLEARANCE_STATE.get("synthetic_since")
        trigger = False
        if stage5_since is not None and (now - float(stage5_since)) >= _COCKPIT_CLEARANCE_HOLD_SEC:
            trigger = True
        if synthetic_since is not None and (now - float(synthetic_since)) >= _COCKPIT_CLEARANCE_HOLD_SEC:
            trigger = True
        if not trigger:
            return False

        _CLEARANCE_STATE["cleared"] = True

    _force_cockpit_live_layout_native()
    _append_terminal(
        "native cockpit-live clearance — splash destroyed from macOS shell",
        level="ok",
    )
    return True


def _cockpit_clearance_monitor_loop() -> None:
    """Background monitor — forces 20/80 split when hydration or STAGE_5 holds 3s+."""
    while _RUNNING.is_set():
        try:
            if not _COCKPIT_EMBEDDED:
                time.sleep(POLL_MS / 1000.0)
                continue
            orch = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/orchestrator_state")
            diag = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/ai_diagnostics")
            status = _read_launcher_status()
            if _tick_cockpit_clearance_override(orch=orch, diag=diag, status=status):
                return
        except Exception as exc:
            log_engine(f"DesktopShell clearance monitor: {type(exc).__name__}: {exc}")
        time.sleep(POLL_MS / 1000.0)


def _ensure_cockpit_clearance_monitor() -> None:
    global _CLEARANCE_MONITOR_STARTED
    with _CLEARANCE_LOCK:
        if _CLEARANCE_MONITOR_STARTED:
            return
        _CLEARANCE_MONITOR_STARTED = True
    threading.Thread(
        target=_cockpit_clearance_monitor_loop,
        name="desktop-cockpit-clearance",
        daemon=True,
    ).start()


def _launcher_ready_for_cockpit(status: dict[str, Any] | None) -> bool:
    """True when launcher has finished verification (stage 8+) or marked ready/gui."""
    if not isinstance(status, dict):
        return False
    stage = str(status.get("stage") or "").lower()
    step = int(status.get("step") or 0)
    if stage in ("gui", "ready"):
        return True
    if step >= 8:
        return True
    if stage == "verify" and step >= 8:
        return True
    return False


def _api_ready_for_cockpit(health: dict[str, Any] | None) -> bool:
    if not isinstance(health, dict):
        return False
    routes = health.get("routes") or {}
    if isinstance(routes, dict) and any(routes.values()):
        return True
    if health.get("port_bound"):
        return True
    return bool(health.get("ok"))


def _orchestrator_ready_for_cockpit(orch: dict[str, Any] | None) -> bool:
    if not isinstance(orch, dict):
        return False
    tokens = orch.get("stage_tokens") or {}
    if isinstance(tokens, dict):
        token = str(tokens.get("STAGE_5_LAUNCH") or "").upper()
        if token in _STAGE5_TOKENS:
            return True
    return bool(orch.get("trade_ready"))


def _should_transition_to_cockpit(
    *,
    status: dict[str, Any] | None,
    health: dict[str, Any] | None,
    orch: dict[str, Any] | None,
    diag: dict[str, Any] | None,
) -> bool:
    """Decide when native splash should hand off to cockpit-web dashboard."""
    if not _api_ready_for_cockpit(health):
        return False
    if _launcher_ready_for_cockpit(status):
        return True
    if _orchestrator_ready_for_cockpit(orch):
        return True
    if _synthetic_hydration_active(diag):
        return True
    stage = str((status or {}).get("stage") or "").lower()
    step = int((status or {}).get("step") or 0)
    detail = str((status or {}).get("detail") or "").lower()
    if step >= 6 and ("complete" in detail or stage in ("warmup", "verify", "gui", "ready")):
        return True
    return stage in _LAUNCHER_COCKPIT_STAGES and step >= 7


def _ensure_cockpit_web_server() -> bool:
    """Wait for embed target — Quantum Terminal (:3000) or legacy Flight Deck (:8787)."""
    if _is_trading_desk_mode():
        health_url = f"http://127.0.0.1:{API_PORT}/api/health"
        terminal_url = f"{TERMINAL_UI_URL_PREFIX}/boot"
        for _ in range(40):
            if _url_alive(health_url) and _url_alive(terminal_url):
                return True
            time.sleep(0.25)
        return _url_alive(health_url) and _url_alive(terminal_url)

    cockpit_url = f"http://127.0.0.1:{COCKPIT_PORT}/"
    if _url_alive(cockpit_url):
        return True
    for _ in range(40):
        if _url_alive(cockpit_url):
            return _cockpit_coupled_to_agent()
        time.sleep(0.25)
    return False


def _cockpit_coupled_to_agent() -> bool:
    """True when :8787 iron_cage reflects agent state (not an orphan stub)."""
    from cockpit.agent_api_proxy import iron_cage_is_agent_coupled

    local = _fetch_json(f"http://127.0.0.1:{COCKPIT_PORT}/api/iron_cage_status")
    agent = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/iron_cage_status")
    return iron_cage_is_agent_coupled(local, agent)


def _handoff_contract_satisfied(
    *,
    status: dict[str, Any] | None,
    health: dict[str, Any] | None,
    orch: dict[str, Any] | None,
) -> bool:
    """
    Mandatory handoff contract — launcher finished + API live must never stall on gate shape.

    When this returns True, coupling checks are bypassed (orphan-stub guard only applies mid-boot).
    """
    if not _api_ready_for_cockpit(health):
        return False
    if _launcher_ready_for_cockpit(status):
        return True
    if isinstance(orch, dict) and orch.get("trade_ready"):
        return True
    return False


def _try_embed_cockpit(url: str, *, force: bool = False, reason: str = "") -> bool:
    """Embed cockpit-web or Quantum Terminal; return True when navigation started."""
    global _HANDOFF_ELIGIBLE_SINCE, _HANDOFF_BLOCK_LOGGED

    if not _url_alive(url):
        return False

    if _is_trading_desk_embed_url(url):
        _transition_to_cockpit(url)
        _HANDOFF_ELIGIBLE_SINCE = None
        _HANDOFF_BLOCK_LOGGED = False
        return True

    if not _ensure_cockpit_web_server():
        return False
    coupled = _cockpit_coupled_to_agent()
    if not force and not coupled:
        return False
    if force and not coupled and reason:
        log_engine(f"DesktopShell handoff override: {reason}")
        _append_terminal(f"handoff override — {reason}", level="warn")
    _transition_to_cockpit(url)
    _HANDOFF_ELIGIBLE_SINCE = None
    _HANDOFF_BLOCK_LOGGED = False
    return True


def _apply_launcher_status(status: dict[str, Any]) -> None:
    from cockpit.launcher_post_ready import launcher_stage_visual_state

    stage = str(status.get("stage") or "")
    step = int(status.get("step") or 0)
    status_line = str(status.get("status") or "")
    detail = str(status.get("detail") or status_line or "")
    tier = str(status.get("boot_tier") or "warming")
    idx = launcher_stage_index(stage, step=step)
    state = launcher_stage_visual_state(
        stage=stage,
        detail=detail,
        boot_tier=tier,
        status=status_line,
    )
    if stage == "failed":
        state = "active"
        tier = "error"
    _evaluate(f"window.__desktopShell && window.__desktopShell.setStage({idx}, '{state}');")
    _evaluate(f"window.__desktopShell && window.__desktopShell.setTier('{_js_escape(tier)}');")
    if detail:
        _evaluate(f"window.__desktopShell && window.__desktopShell.setStatus('{_js_escape(detail)}');")


def _apply_orchestrator_tokens(tokens: dict[str, Any] | None, orch: dict[str, Any] | None = None) -> None:
    states = orchestrator_segment_states(tokens)
    for i, seg_state in enumerate(states):
        _evaluate(
            f"window.__desktopShell && window.__desktopShell.setOrchSegment({i}, '{seg_state}');"
        )
    _push_boot_stage_matrix(orch, tokens)


def _push_boot_stage_matrix(
    orch: dict[str, Any] | None,
    tokens: dict[str, Any] | None = None,
) -> None:
    """Push sanitized 9-stage RAG matrix into embedded Flight Deck."""
    try:
        from cockpit.ui_payload_sanitizer import sanitize_orchestrator_for_ui

        safe = sanitize_orchestrator_for_ui(orch or {"stage_tokens": tokens or {}})
        tok = json.dumps(safe.get("stage_tokens") or {})
        status = json.dumps(safe.get("stage_status") or {})
        errors = json.dumps(safe.get("stage_errors") or {})
        _evaluate(
            f"window.__flightDeck && window.__flightDeck.applyStageTokens({tok},{status},{errors});"
        )
    except Exception as exc:
        log_engine(f"DesktopShell boot stage matrix push: {type(exc).__name__}: {exc}")


def _push_autonomic_recovery_to_cockpit(
    orch: dict[str, Any] | None,
    diag: dict[str, Any] | None,
    iron: dict[str, Any] | None = None,
) -> None:
    if not diag:
        return
    synthetic = bool(diag.get("synthetic_hydration_active"))
    tier = str(diag.get("fallback_transport_tier") or "").upper()
    if not synthetic and "REST_POLL" not in tier:
        return
    try:
        from cockpit.ui_payload_sanitizer import sanitize_recovery_payload

        payload = json.dumps(sanitize_recovery_payload(orch=orch, diag=diag, iron=iron))
        _evaluate(
            f"window.__flightDeck && window.__flightDeck.applyAutonomicRecovery({payload});"
        )
    except Exception as exc:
        log_engine(f"DesktopShell autonomic recovery push: {type(exc).__name__}: {exc}")


class _LogTailState:
    def __init__(self) -> None:
        self.offsets: dict[str, int] = {}


_LOG_TAIL = _LogTailState()


def _poll_log_tails() -> None:
    for path in _log_paths():
        key = str(path)
        try:
            size = path.stat().st_size
            start = _LOG_TAIL.offsets.get(key, max(0, size - 8192))
            if size < start:
                start = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                chunk = fh.read()
                _LOG_TAIL.offsets[key] = fh.tell()
            for line in chunk.splitlines()[-12:]:
                line = line.strip()
                if line:
                    _append_terminal(f"[{path.name}] {line}")
        except OSError:
            continue


def _micro_health_ping_with_retry() -> dict[str, Any]:
    """
    Stage 6/9 telemetry micro-health ping — 5× retry @ 1000ms, safe WARMING fallback.

    Never raises; port collisions during smoke tests must not crash the shell.
    """
    endpoints: tuple[tuple[str, str], ...] = (
        ("bootstrap", f"http://127.0.0.1:{API_PORT}/health"),
        ("health_light", f"http://127.0.0.1:{API_PORT}/api/health_light"),
        ("health", f"http://127.0.0.1:{API_PORT}/api/health"),
        ("orchestrator", f"http://127.0.0.1:{API_PORT}/api/orchestrator_state"),
        ("ai_diagnostics", f"http://127.0.0.1:{API_PORT}/api/ai_diagnostics"),
    )
    outcome: dict[str, Any] = {"ok": False, "tier": "WARMING", "routes": {}, "attempts": 0}

    for attempt in range(1, _HEALTH_RETRY_MAX + 1):
        outcome["attempts"] = attempt
        try:
            routes: dict[str, bool] = {}
            for name, url in endpoints:
                try:
                    routes[name] = _fetch_json(url) is not None
                except Exception as exc:
                    log_engine(f"DesktopShell route {name} ping: {type(exc).__name__}: {exc}")
                    routes[name] = False
            outcome["routes"] = routes
            if any(routes.values()):
                outcome["ok"] = True
                outcome["tier"] = "LIVE"
                return outcome
        except Exception as exc:
            log_engine(
                f"DesktopShell micro-health ping attempt {attempt}/{_HEALTH_RETRY_MAX}: "
                f"{type(exc).__name__}: {exc}"
            )
        if attempt < _HEALTH_RETRY_MAX:
            time.sleep(_HEALTH_RETRY_MS / 1000.0)

    try:
        from cockpit.desktop_process_guard import port_is_bound

        _evaluate("window.__desktopShell && window.__desktopShell.setTier('warming');")
        if port_is_bound(API_PORT):
            outcome["port_bound"] = True
            outcome["tier"] = "HYDRATING"
            _append_terminal(
                "micro-health ping deferred — agent bound, hydration continuing",
                level="warn",
            )
        else:
            _append_terminal(
                "micro-health ping deferred — holding WARMING (port collision or boot delay)",
                level="warn",
            )
    except Exception:
        pass
    return outcome


def _status_poll_loop() -> None:
    global _HANDOFF_ELIGIBLE_SINCE, _HANDOFF_BLOCK_LOGGED

    last_stage = ""
    stage = ""
    cockpit_url = _resolve_embed_cockpit_url()
    while _RUNNING.is_set():
        try:
            status = _read_launcher_status()
            if status:
                stage = str(status.get("stage") or "")
                if stage != last_stage:
                    last_stage = stage
                    _apply_launcher_status(status)
                    _append_terminal(
                        f"launcher stage={stage} step={status.get('step')} "
                        f":: {status.get('detail', '')}"
                    )

            health = _micro_health_ping_with_retry()
            orch: dict[str, Any] | None = None
            diag: dict[str, Any] | None = None
            if health.get("ok"):
                orch = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/orchestrator_state")
                if orch:
                    _apply_orchestrator_tokens(orch.get("stage_tokens") or {}, orch)

                diag = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/ai_diagnostics")
                if diag:
                    tier = str(diag.get("fallback_transport_tier") or "")
                    global _SYNTH_HYDRATION_LOGGED
                    if diag.get("synthetic_hydration_active") and not _SYNTH_HYDRATION_LOGGED:
                        _SYNTH_HYDRATION_LOGGED = True
                        _append_terminal(
                            "synthetic hydration active — alpha gate primed",
                            level="warn",
                        )
                    if tier and tier != "live":
                        _evaluate(
                            "window.__desktopShell && window.__desktopShell.setTier('rest_poll');"
                        )
                    iron_snap = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/iron_cage_status")
                    _push_autonomic_recovery_to_cockpit(orch, diag, iron_snap)

            if _COCKPIT_EMBEDDED:
                _tick_cockpit_clearance_override(
                    orch=orch,
                    diag=diag,
                    status=status,
                )
            elif _should_transition_to_cockpit(
                status=status,
                health=health,
                orch=orch,
                diag=diag,
            ):
                now = time.monotonic()
                if _HANDOFF_ELIGIBLE_SINCE is None:
                    _HANDOFF_ELIGIBLE_SINCE = now

                contract = _handoff_contract_satisfied(
                    status=status,
                    health=health,
                    orch=orch,
                )
                if _try_embed_cockpit(
                    cockpit_url,
                    force=contract,
                    reason="launcher_contract" if contract else "",
                ):
                    return

                elapsed = now - (_HANDOFF_ELIGIBLE_SINCE or now)
                if not _HANDOFF_BLOCK_LOGGED and elapsed >= _HANDOFF_BLOCK_WARN_SEC:
                    _HANDOFF_BLOCK_LOGGED = True
                    coupled = _cockpit_coupled_to_agent()
                    _append_terminal(
                        f"handoff delayed {elapsed:.0f}s — coupled={coupled} "
                        f"cockpit_up={_url_alive(cockpit_url)}",
                        level="warn",
                    )
                if elapsed >= _HANDOFF_FORCE_SEC and _try_embed_cockpit(
                    cockpit_url,
                    force=True,
                    reason=f"watchdog_{elapsed:.0f}s",
                ):
                    return
            else:
                _HANDOFF_ELIGIBLE_SINCE = None
                _HANDOFF_BLOCK_LOGGED = False
        except Exception as exc:
            log_engine(f"DesktopShell status poll guard: {type(exc).__name__}: {exc}")
            _append_terminal(f"status poll guard: {type(exc).__name__}", level="warn")

        time.sleep(POLL_MS / 1000.0)


def _log_poll_loop() -> None:
    while _RUNNING.is_set():
        _poll_log_tails()
        time.sleep(LOG_POLL_MS / 1000.0)


def _transition_to_cockpit(url: str) -> None:
    import webview  # noqa: WPS433

    global _WINDOW, _COCKPIT_EMBEDDED
    bust_url = _cache_bust_cockpit_url(url)
    use_load_url = _should_use_load_url(bust_url)
    label = "Quantum Terminal LIVE" if use_load_url else "Flight Deck LIVE"
    _append_terminal(f"{label} — embedding {bust_url}", level="ok")
    _evaluate("window.__desktopShell && window.__desktopShell.setTier('live');")
    try:
        win = _WINDOW or (webview.windows[0] if webview.windows else None)
        if win is not None:
            if use_load_url:
                win.load_url(bust_url)
                _append_terminal("Quantum Terminal loaded via load_url", level="ok")
                _evaluate("window.__IG_TRADING_DESK_NATIVE__ = true;")
            else:
                html = _read_cache_busted_cockpit_html(base_url=bust_url)
                base_uri = bust_url
                if html is not None:
                    win.load_html(html, base_uri=base_uri)
                    _append_terminal("cache-busted index.html injected from disk", level="ok")
                else:
                    win.load_url(bust_url)
        _COCKPIT_EMBEDDED = True
        _ensure_cockpit_clearance_monitor()
        threading.Thread(
            target=_post_cockpit_load_clearance,
            name="desktop-post-cockpit-clearance",
            daemon=True,
        ).start()
    except Exception as exc:
        log_engine(f"DesktopShell cockpit transition failed: {type(exc).__name__}: {exc}")


def _post_cockpit_load_clearance() -> None:
    """Re-apply native clearance after WKWebView navigation settles."""
    for delay in (0.8, 1.6, 3.2):
        if not _RUNNING.is_set():
            return
        time.sleep(delay)
        try:
            orch = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/orchestrator_state")
            diag = _fetch_json(f"http://127.0.0.1:{API_PORT}/api/ai_diagnostics")
            status = _read_launcher_status()
            if _tick_cockpit_clearance_override(orch=orch, diag=diag, status=status):
                return
            if delay >= 3.2 and _COCKPIT_EMBEDDED:
                with _CLEARANCE_LOCK:
                    if not _CLEARANCE_STATE.get("cleared"):
                        _CLEARANCE_STATE["cleared"] = True
                _force_cockpit_live_layout_native()
                _append_terminal("post-load native clearance fallback engaged", level="warn")
        except Exception as exc:
            log_engine(f"DesktopShell post-load clearance: {type(exc).__name__}: {exc}")


def _spawn_launcher_supervisor() -> None:
    global _LAUNCHER_PROC
    script = _project_root() / "macos" / "launcher" / "igagent_launcher.sh"
    if not script.is_file():
        _append_terminal(f"launcher script missing: {script}", level="err")
        return
    env = {**os.environ, "IG_AGENT_ROOT": str(_project_root()), "LAUNCHER_DESKTOP": "1", "IG_DESKTOP_SHELL_ACTIVE": "1"}
    _LAUNCHER_PROC = subprocess.Popen(
        ["/bin/bash", str(script)],
        cwd=str(_project_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    register_child_process(_LAUNCHER_PROC)
    _append_terminal(f"supervisor pid={_LAUNCHER_PROC.pid} engaged", level="ok")

    def _pipe_launcher_output() -> None:
        proc = _LAUNCHER_PROC
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            if not _RUNNING.is_set():
                break
            _append_terminal(line.rstrip())

    threading.Thread(target=_pipe_launcher_output, name="launcher-stdout", daemon=True).start()


def _on_loaded() -> None:
    _append_terminal("Iron Cage desktop shell initialized", level="ok")
    _append_terminal(f"canvas {WINDOW_WIDTH_DEFAULT}x{WINDOW_HEIGHT_DEFAULT} · background {BACKGROUND}")
    threading.Thread(target=_status_poll_loop, name="desktop-status-poll", daemon=True).start()
    threading.Thread(target=_log_poll_loop, name="desktop-log-poll", daemon=True).start()


def _on_closing() -> bool:
    _RUNNING.clear()
    global _LAUNCHER_PROC
    if _LAUNCHER_PROC is not None and _LAUNCHER_PROC.poll() is None:
        register_child_pid(int(_LAUNCHER_PROC.pid))
    if _LAUNCHER_PROC is not None and _LAUNCHER_PROC.poll() is None:
        try:
            _LAUNCHER_PROC.terminate()
        except OSError:
            pass
    execute_sequential_desktop_teardown(root=_project_root(), deadline_sec=1.0)
    return True


def _request_graceful_exit(*, source: str = "ui") -> None:
    """Close WKWebView and run the same teardown path as the window closing hook."""
    log_engine(f"DesktopShell graceful exit requested (source={source})")
    _append_terminal("graceful exit — closing Flight Deck and running anti-zombie teardown", level="ok")

    def _work() -> None:
        try:
            import webview  # noqa: WPS433

            win = _WINDOW
            if win is None and webview.windows:
                win = webview.windows[0]
            if win is not None:
                win.destroy()
                return
        except Exception as exc:
            log_engine(f"DesktopShell destroy window: {type(exc).__name__}: {exc}")
        _on_closing()
        try:
            import webview  # noqa: WPS433

            webview.destroy_window()
        except Exception:
            os._exit(0)

    threading.Thread(target=_work, name="desktop-graceful-exit", daemon=True).start()


class DesktopShellApi:
    """Minimal JS bridge — graceful exit + diagnostics."""

    def graceful_exit(self) -> str:
        _request_graceful_exit(source="pywebview_api")
        return "exiting"

    def emergency_exit(self) -> str:
        """Legacy alias — same as graceful_exit (closes shell, does not flatten positions)."""
        return self.graceful_exit()

    def read_launcher_status(self) -> dict[str, Any]:
        return _read_launcher_status() or {}


def run_smoke_test() -> int:
    html = build_splash_html()
    if "stage-checklist" not in html or len(LAUNCHER_STAGES) != 9:
        print("SMOKE FAIL: splash HTML incomplete", file=sys.stderr)
        return 1
    bust = _cache_bust_cockpit_url(f"http://127.0.0.1:{COCKPIT_PORT}/")
    if "v=" not in bust or "_cb=" not in bust:
        print("SMOKE FAIL: cache-bust URL malformed", file=sys.stderr)
        return 1
    if "cockpit-live" not in _FORCE_COCKPIT_LIVE_JS:
        print("SMOKE FAIL: clearance JS incomplete", file=sys.stderr)
        return 1
    try:
        import webview  # noqa: F401
    except ImportError:
        print("SMOKE FAIL: pip install pywebview", file=sys.stderr)
        return 1
    from cockpit.desktop_process_guard import reset_desktop_process_guard_for_tests

    reset_desktop_process_guard_for_tests()
    summary = execute_anti_zombie_shutdown(
        root=_project_root(),
        skip_mark_manual_stop=True,
    )
    if not summary.get("ok"):
        print(f"SMOKE FAIL: anti-zombie {summary}", file=sys.stderr)
        return 1
    print(
        f"SMOKE OK shell=desktop_app_shell stages={len(LAUNCHER_STAGES)} "
        f"html_bytes={len(html)} anti_zombie_ok=True"
    )
    return 0


def run_gui(*, launch_supervisor: bool = False, cockpit_url: str | None = None) -> int:
    try:
        import webview  # noqa: WPS433
    except ImportError:
        print("ERROR: pywebview required — .venv/bin/pip install pywebview", file=sys.stderr)
        return 1

    os.environ["IG_DESKTOP_SHELL_ACTIVE"] = "1"
    trading_desk = _is_trading_desk_mode()
    if trading_desk:
        os.environ["IG_DESKTOP_TRADING_DESK"] = "1"
    else:
        os.environ["IG_DESKTOP_FLIGHT_DECK"] = "1"

    if trading_desk:
        log_engine("DesktopShell: Trading Desk mode — preserving live agent on :8080")
    else:
        purge = audit_and_purge_bound_ports(preserve_pid=os.getpid())
        log_engine(f"DesktopShell cold-start port audit: {purge.get('purged_pids', [])}")

    global _WINDOW
    _RUNNING.set()
    api = DesktopShellApi()
    splash = build_splash_html(trading_desk=trading_desk)

    _WINDOW = webview.create_window(
        WINDOW_TITLE,
        html=splash,
        js_api=api,
        width=WINDOW_WIDTH_DEFAULT,
        height=WINDOW_HEIGHT_DEFAULT,
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        background_color=BACKGROUND,
        frameless=True,
        easy_drag=True,
    )
    _WINDOW.events.loaded += _on_loaded
    _WINDOW.events.closing += _on_closing

    if launch_supervisor:
        threading.Thread(target=_spawn_launcher_supervisor, name="launcher-spawn", daemon=True).start()

    target = (cockpit_url or os.environ.get("IG_COCKPIT_URL", "")).strip()
    if not target and trading_desk:
        target = DEFAULT_TRADING_DESK_URL
    if target and _url_alive(target):
        delay = 1.2 if trading_desk else 0.6

        def _fast_load() -> None:
            time.sleep(delay)
            _transition_to_cockpit(target)

        threading.Thread(target=_fast_load, name="cockpit-fast-load", daemon=True).start()

    try:
        webview.start(gui="coco", debug=False)
    except Exception as exc:
        log_engine(f"DesktopShell webview start failed: {type(exc).__name__}: {exc}")
        _on_closing()
        return 1
    finally:
        _RUNNING.clear()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Iron Cage Flight Deck desktop shell")
    parser.add_argument("--smoke-test", action="store_true", help="Validate assets without opening GUI")
    parser.add_argument(
        "--launch-supervisor",
        action="store_true",
        help="Spawn macos/launcher/igagent_launcher.sh in background",
    )
    parser.add_argument(
        "--cockpit-url",
        default="",
        help="Optional Flight Deck URL (default http://127.0.0.1:8787/)",
    )
    args = parser.parse_args(argv)
    if args.smoke_test:
        return run_smoke_test()
    if args.cockpit_url.strip():
        url = args.cockpit_url.strip()
    elif _is_trading_desk_mode():
        url = os.environ.get("IG_COCKPIT_URL", "").strip() or DEFAULT_TRADING_DESK_URL
    else:
        url = f"http://127.0.0.1:{COCKPIT_PORT}/"
    return run_gui(launch_supervisor=bool(args.launch_supervisor), cockpit_url=url)


if __name__ == "__main__":
    raise SystemExit(main())
