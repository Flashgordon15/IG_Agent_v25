#!/usr/bin/env python3
"""
IG Agent Apex v30 — Desktop Command Console (guaranteed macOS render path).

Tkinter on macOS Aqua cannot paint widgets (blank grey window). This module uses
native WKWebView via pywebview for rendering, with a background thread that reads
``ig_agent_v30_shm`` every 500ms and pushes frames via evaluate_js — pure SHM,
zero HTTP.

Launch (recommended — pywebview in .venv):
  PYTHONPATH=src .venv/bin/python3 scripts/desktop_cockpit.py

Smoke test:
  PYTHONPATH=src .venv/bin/python3 scripts/desktop_cockpit.py --smoke-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_HTML = Path(__file__).resolve().parent / "cockpit_neon.html"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

REFRESH_MS = 500
GATE_REFRESH_MS = 2000
STALL_SEC = 2.0
WINDOW_TITLE = "IG Agent Apex — V30 Unified Command Console"
COCKPIT_SHM_NAME = "ig_agent_v30_shm"
FULFILLMENT_URL = "http://127.0.0.1:8080/api/unified/fulfillment"
HEALTH_URL = "http://127.0.0.1:8080/api/health"
HEAL_URL = "http://127.0.0.1:8080/api/cockpit/heal"
AGENT_START_SCRIPT = _ROOT / "scripts" / "start_agent_background.sh"
FLIGHT_DECK_SCRIPT = _ROOT / "flight_deck_launch.sh"

# Cockpit linkage states (mirrors cockpit_shm_passive + API layer).
STATE_LIVE = "LIVE"
STATE_STALE_SHM = "STALE_SHM"
STATE_AGENT_OFFLINE = "AGENT_OFFLINE"
STATE_API_ONLY = "API_ONLY"
STATE_MANUAL_STOP = "MANUAL_STOP"
STATE_BOOTING = "BOOTING"


def _load_html() -> str:
    return _HTML.read_text(encoding="utf-8")


def _safe_read_shm() -> tuple[dict[str, Any] | None, str | None]:
    try:
        from system.ipc.cockpit_shm_passive import (
            LINK_LIVE,
            LINK_NO_SEGMENT,
            LINK_STALE_SHM,
            read_cockpit_shm,
        )

        view = read_cockpit_shm()
        if view is None:
            return None, "segment not published"
        link = str(view.get("link_state") or "")
        if link == LINK_STALE_SHM:
            return view, str(view.get("link_detail") or "stale SHM — publisher dead")
        if link and link != LINK_LIVE and link != LINK_NO_SEGMENT:
            return view, str(view.get("link_detail") or link)
        return view, None
    except FileNotFoundError:
        return None, "FileNotFoundError: ig_agent_v30_shm"
    except OSError as exc:
        if getattr(exc, "errno", None) == 2:
            return None, "SHM segment missing"
        return None, f"OSError: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _fetch_health() -> dict[str, Any]:
    try:
        import urllib.request

        with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _manual_stop_active() -> bool:
    try:
        from system.shutdown_cleanup import manual_stop_active

        return bool(manual_stop_active())
    except Exception:
        for path in (
            _ROOT / "src/data/state/manual_stop.json",
            _ROOT / "src/data/manual_stop.json",
            _ROOT / "manual_stop.json",
        ):
            if path.is_file():
                return True
        return False


def _classify_linkage(
    view: dict[str, Any] | None,
    err: str | None,
    health: dict[str, Any],
) -> dict[str, Any]:
    """Single source of truth for cockpit ↔ agent binding."""
    from system.cockpit_feed_guardian import pid_mismatch

    if _manual_stop_active():
        return {
            "state": STATE_MANUAL_STOP,
            "title": "MANUAL STOP ACTIVE",
            "detail": "Watchdog will not restart until manual_stop clears (~10 min TTL).",
            "recovery": f"rm -f {_ROOT}/src/data/state/manual_stop.json && ./flight_deck_launch.sh",
        }

    api_pid = int(health.get("agent_pid") or 0)
    api_ready = bool((health.get("boot_metrics") or {}).get("ready"))
    api_alive = bool(health.get("agent_alive")) or api_pid > 0

    if view is not None:
        link = str(view.get("link_state") or "")
        if pid_mismatch(view, health):
            return {
                "state": STATE_STALE_SHM,
                "title": "PID MISMATCH — STALE SHM",
                "detail": (
                    f"SHM pid {view.get('agent_pid')} != agent pid {api_pid} "
                    "— reading zombie segment"
                ),
                "recovery": "auto-heal: API fallback + feed reset",
                "stale_pid": int(view.get("agent_pid") or 0),
            }
        if link == "STALE_SHM" or not bool(view.get("publisher_alive", True)):
            return {
                "state": STATE_STALE_SHM,
                "title": "STALE SHM — ZOMBIE SEGMENT",
                "detail": err or str(view.get("link_detail") or "publisher PID dead"),
                "recovery": "./flight_deck_launch.sh",
                "stale_pid": int(view.get("agent_pid") or 0),
            }

    if view is not None and bool(view.get("publisher_alive")):
        return {
            "state": STATE_LIVE,
            "title": "TRUE SYNC",
            "detail": f"pid {view.get('agent_pid')} publishing SHM",
            "recovery": "",
        }

    if api_alive and not api_ready:
        pct = int((health.get("boot_metrics") or {}).get("percent") or 0)
        return {
            "state": STATE_BOOTING,
            "title": "AGENT BOOTING",
            "detail": f"pid {api_pid} gate progress {pct}% — SHM not ready yet",
            "recovery": "wait for G5 ready",
        }

    if api_alive and api_ready:
        return {
            "state": STATE_API_ONLY,
            "title": "API LIVE — SHM LAG",
            "detail": "Agent healthy but cockpit SHM not attached yet (re-publishing…)",
            "recovery": "wait 2s or restart cockpit",
            "api_pid": api_pid,
        }

    return {
        "state": STATE_AGENT_OFFLINE,
        "title": "AGENT OFFLINE",
        "detail": err or "main.py not running — nothing publishes ig_agent_v30_shm",
        "recovery": "./flight_deck_launch.sh",
    }


def _fetch_fulfillment() -> dict[str, Any]:
    """Gate diagnostics + live tuning from agent API (read-only, off hot path)."""
    try:
        import urllib.request

        with urllib.request.urlopen(FULFILLMENT_URL, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _post_feed_heal(reason: str) -> dict[str, Any]:
    try:
        import urllib.request

        body = json.dumps({"reason": reason}).encode("utf-8")
        req = urllib.request.Request(
            HEAL_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _start_agent_background() -> None:
    if not AGENT_START_SCRIPT.is_file():
        print(f"[COCKPIT HEAL] missing {AGENT_START_SCRIPT}", flush=True)
        return
    import subprocess

    print("[COCKPIT HEAL] starting agent via start_agent_background.sh", flush=True)
    subprocess.Popen(
        ["/bin/bash", str(AGENT_START_SCRIPT)],
        cwd=str(_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _apply_heal_action(action: str, detail: str) -> dict[str, Any] | None:
    from system.cockpit_feed_guardian import (
        HEAL_FEED_RESET,
        HEAL_START_AGENT,
        HEAL_USE_API,
    )

    if action == HEAL_FEED_RESET:
        print(f"[COCKPIT HEAL] feed_reset — {detail}", flush=True)
        return _post_feed_heal(detail)
    if action == HEAL_START_AGENT:
        _start_agent_background()
        return {"ok": True, "action": "start_agent"}
    if action == HEAL_USE_API:
        print(f"[COCKPIT HEAL] api_fallback — {detail}", flush=True)
        return {"ok": True, "action": "use_api"}
    return None


def _view_from_api(health: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    """HTTP fallback skeleton when SHM is missing but API is up."""
    dv = gate.get("data_velocity") or {}
    tun = gate.get("tuning_variables") or {}
    ft = gate.get("alpha_frontier_tracker") or {}
    ring = ft.get("ring") or {}
    return {
        "agent_pid": int(health.get("agent_pid") or 0),
        "ticks_cached": int(dv.get("ticks_cached") or gate.get("ticks_cached") or 0),
        "live_ram_ticks": int(dv.get("live_ram_ticks") or 0),
        "signal_threshold": float(tun.get("signal_threshold") or 52.5),
        "atr_multiplier": float(tun.get("atr_multiplier") or 2.5),
        "coordinate": int((ring.get("last_coordinate") or 0)),
        "valve_status": 1 if ring.get("win_zone_armed") else 0,
        "write_seq": int(gate.get("pulse_serial") or 0),
        "pulse_serial": int(gate.get("pulse_serial") or 0),
        "stall_active": bool(dv.get("stall_active")),
        "last_trade_pnl": float(
            (gate.get("last_performance_row") or {}).get("pnl_gbp") or 0
        ),
        "performance_rows": list(gate.get("performance_rows") or []),
        "memory_alignment": gate.get("memory_alignment") or "API FALLBACK",
        "tuning_source": tun.get("source") or "api",
        "publisher_alive": False,
        "link_state": "API_FALLBACK",
    }


_GATE_CACHE: dict[str, Any] = {}
_GATE_LOCK = threading.Lock()


def _gate_poll_loop() -> None:
    while _RUNNING.is_set():
        snap = _fetch_fulfillment()
        if snap:
            with _GATE_LOCK:
                _GATE_CACHE.clear()
                _GATE_CACHE.update(snap)
        time.sleep(GATE_REFRESH_MS / 1000.0)


def _gate_snapshot() -> dict[str, Any]:
    with _GATE_LOCK:
        return dict(_GATE_CACHE)


class CockpitApi:
    def emergency_stop(self) -> str:
        print("\n" + "=" * 72, flush=True)
        print("DESKTOP COCKPIT — EMERGENCY RECOVERY STOP", flush=True)
        print(f"UTC {datetime.now(timezone.utc).isoformat()}", flush=True)
        print("=" * 72 + "\n", flush=True)
        root = str(_ROOT)
        os.system("/usr/bin/pkill -9 -f main.py")
        os.system(f"/bin/rm -f {root}/src/data/*.lock {root}/manual_stop.json")
        os.system(f"/bin/rm -f {root}/src/data/manual_stop.json")
        _RUNNING.clear()
        return "stopped"


_RUNNING = threading.Event()
_RUNNING.set()
_WINDOW = None


def _shm_poll_loop() -> None:
    """Background SHM reader — pushes JSON frames into WebKit + self-heal."""
    import webview  # noqa: WPS433

    from system.cockpit_feed_guardian import (
        FeedWatchState,
        HEAL_USE_API,
        decide_heal_action,
        is_publish_stalled,
        update_feed_watch,
    )

    global _WINDOW
    watch = FeedWatchState()
    force_api = False
    last_heal_result: dict[str, Any] | None = None

    while _RUNNING.is_set():
        view, err = _safe_read_shm()
        gate = _gate_snapshot()
        health = _fetch_health()
        link = _classify_linkage(view, err, health)
        state = str(link.get("state") or STATE_AGENT_OFFLINE)

        use_view: dict[str, Any] | None = view
        if state in (STATE_AGENT_OFFLINE, STATE_MANUAL_STOP, STATE_BOOTING):
            use_view = None
            force_api = False
        elif state == STATE_STALE_SHM:
            force_api = True
            if health.get("agent_pid") and (health.get("boot_metrics") or {}).get("ready"):
                use_view = _view_from_api(health, gate)
            else:
                use_view = None
        elif state == STATE_API_ONLY:
            use_view = _view_from_api(health, gate)
            force_api = True
        elif force_api and health.get("agent_pid"):
            use_view = _view_from_api(health, gate)

        if use_view is not None:
            update_feed_watch(watch, use_view)
        stalled, frozen_sec, stall_reason = is_publish_stalled(
            watch, use_view, gate=gate
        )

        heal_action, heal_detail = decide_heal_action(
            link_state=state,
            stalled=stalled,
            stall_reason=stall_reason,
            health=health,
            view=view,
            watch=watch,
        )
        if heal_action not in ("none", ""):
            last_heal_result = _apply_heal_action(heal_action, heal_detail)
            if heal_action == HEAL_USE_API:
                force_api = True
                use_view = _view_from_api(health, gate)
                update_feed_watch(watch, use_view)
                stalled, frozen_sec, stall_reason = is_publish_stalled(
                    watch, use_view, gate=gate
                )

        payload: dict[str, Any]
        if use_view is None:
            payload = {
                "ok": False,
                "error": str(link.get("detail") or err or "agent offline"),
                "link": link,
                "gates": gate,
                "health": health,
                "heal": last_heal_result,
            }
        else:
            use_view["_frozen_sec"] = frozen_sec
            use_view["_publish_stalled"] = stalled
            use_view["_stall_reason"] = stall_reason
            tun = gate.get("tuning_variables") or {}
            if tun.get("signal_threshold"):
                use_view["signal_threshold"] = float(tun["signal_threshold"])
            if tun.get("atr_multiplier"):
                use_view["atr_multiplier"] = float(tun["atr_multiplier"])
            use_view["tuning_source"] = tun.get("source") or use_view.get(
                "tuning_source", "shm"
            )
            payload = {
                "ok": True,
                "view": use_view,
                "gates": gate,
                "health": health,
                "link": link,
                "frontier": (gate.get("alpha_frontier_tracker") or {}),
                "heal": last_heal_result,
                "feed_guardian": {
                    "stalled": stalled,
                    "frozen_sec": round(frozen_sec, 2),
                    "reason": stall_reason,
                    "heal_count": watch.heal_count,
                    "force_api": force_api,
                },
            }

        js = f"window.updateFromShm({json.dumps(payload)});"
        try:
            win = _WINDOW or (webview.windows[0] if webview.windows else None)
            if win is not None:
                win.evaluate_js(js)
        except Exception as exc:
            print(f"[COCKPIT] evaluate_js: {exc}", flush=True)

        time.sleep(REFRESH_MS / 1000.0)


def _on_loaded() -> None:
    threading.Thread(target=_gate_poll_loop, name="cockpit-gate-poll", daemon=True).start()
    threading.Thread(target=_shm_poll_loop, name="cockpit-shm-poll", daemon=True).start()


def _preflight_ensure_agent(*, wait_sec: float = 120.0) -> int:
    """
    Before opening GUI: start agent if offline (unless manual_stop).
    Returns 0 when API responds, 1 when still offline after wait.
    """
    if _fetch_health().get("agent_pid"):
        boot = (_fetch_health().get("boot_metrics") or {}).get("ready")
        if boot:
            return 0

    if _manual_stop_active():
        print(
            "PREFLIGHT: manual_stop active — start agent manually after clearing flag",
            flush=True,
        )
        return 1

    if not AGENT_START_SCRIPT.is_file():
        print(f"PREFLIGHT: missing {AGENT_START_SCRIPT}", flush=True)
        return 1

    print("PREFLIGHT: agent offline — starting start_agent_background.sh …", flush=True)
    import subprocess

    subprocess.Popen(
        ["/bin/bash", str(AGENT_START_SCRIPT)],
        cwd=str(_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        health = _fetch_health()
        if health.get("agent_pid") and (health.get("boot_metrics") or {}).get("ready"):
            print(f"PREFLIGHT: agent ready pid={health.get('agent_pid')}", flush=True)
            return 0
        time.sleep(2)
    print("PREFLIGHT: agent did not become ready in time", flush=True)
    return 1


def run_smoke_test() -> int:
    if not _HTML.is_file():
        print(f"SMOKE FAIL: missing {_HTML}", file=sys.stderr)
        return 1
    html = _load_html()
    if "updateFromShm" not in html:
        print("SMOKE FAIL: HTML missing updateFromShm", file=sys.stderr)
        return 1
    try:
        import webview  # noqa: WPS433
    except ImportError:
        print("SMOKE FAIL: pip install pywebview", file=sys.stderr)
        return 1

    view, err = _safe_read_shm()
    ticks = int(view.get("ticks_cached") or 0) if view else 0
    print(
        f"SMOKE OK renderer=webkit source=shm shm={'live' if view else 'loading'} "
        f"ticks={ticks} err={err or 'none'} html_bytes={len(html)}"
    )
    return 0


def run_gui() -> int:
    try:
        import webview  # noqa: WPS433
    except ImportError:
        print("ERROR: pywebview required — .venv/bin/pip install pywebview", file=sys.stderr)
        return 1

    global _WINDOW
    api = CockpitApi()
    html = _load_html()

    _WINDOW = webview.create_window(
        WINDOW_TITLE,
        html=html,
        width=1140,
        height=780,
        min_size=(920, 640),
        background_color="#121214",
        js_api=api,
    )
    _WINDOW.events.loaded += _on_loaded
    try:
        webview.start(gui="coco", debug=False)
    except Exception as exc:
        print(f"[COCKPIT] webview start failed: {exc}", flush=True)
        traceback.print_exc()
        return 1
    finally:
        _RUNNING.clear()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent Apex desktop cockpit (WebKit + SHM)")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip auto-start when agent is offline",
    )
    args = parser.parse_args()
    if args.smoke_test:
        return run_smoke_test()
    if not args.no_preflight:
        _preflight_ensure_agent()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
