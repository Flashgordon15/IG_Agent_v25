"""Lightweight local web cockpit — WebSocket telemetry hub (2.5 Hz)."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from system.engine_log import log_engine

DEFAULT_COCKPIT_PORT = 8787
_server: Any | None = None
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_stop = threading.Event()
_lock = threading.Lock()

# Shared latest payload for multi-client fan-out without re-draining queue per client
_latest_payload: dict[str, Any] = {}
_latest_lock = threading.Lock()
_hot_reload_pending = False
_hot_reload_lock = threading.Lock()
_log_tail_offset = 0
_log_tail_lock = threading.Lock()


def cockpit_web_root() -> Path:
    from system.paths import project_root

    return project_root() / "cockpit-web"


def _drain_telemetry_queue() -> dict[str, Any] | None:
    from cockpit.telemetry_bridge import get_telemetry_queue

    tq = get_telemetry_queue()
    latest: dict[str, Any] | None = None
    try:
        while True:
            latest = tq.get_nowait()
    except queue.Empty:
        pass
    except Exception:
        return None
    if latest is not None:
        with _latest_lock:
            _latest_payload.clear()
            _latest_payload.update(latest)
    return latest


def get_latest_telemetry() -> dict[str, Any]:
    _drain_telemetry_queue()
    with _latest_lock:
        return dict(_latest_payload)


def broadcast_system_hot_reload(*, source: str = "supervisor") -> None:
    """Queue a SYSTEM_HOT_RELOAD frame for all /ws/telemetry clients."""
    global _hot_reload_pending
    with _hot_reload_lock:
        _hot_reload_pending = True
    log_engine(f"Flight Deck: SYSTEM_HOT_RELOAD queued (source={source})")


def _pop_hot_reload_frame() -> dict[str, Any] | None:
    global _hot_reload_pending
    with _hot_reload_lock:
        if not _hot_reload_pending:
            return None
        _hot_reload_pending = False
    return {
        "type": "SYSTEM_HOT_RELOAD",
        "ts": time.time(),
        "source": "supervisor",
    }


def _read_engine_log_batch(*, max_lines: int = 24) -> list[str]:
    """Tail new lines from engine.log for the avionics HUD."""
    global _log_tail_offset
    from system.paths import logs_dir

    path = logs_dir() / "engine.log"
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with _log_tail_lock:
            if _log_tail_offset > size:
                _log_tail_offset = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(_log_tail_offset)
                chunk = fh.read()
                _log_tail_offset = fh.tell()
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines
    except OSError:
        return []


def create_cockpit_app() -> Any:
    web_root = cockpit_web_root()
    app = FastAPI(title="IG Agent Flight Deck", version="29.1", docs_url=None, redoc_url=None)

    if web_root.is_dir():
        app.mount("/static", StaticFiles(directory=str(web_root)), name="cockpit-static")

    @app.get("/")
    async def cockpit_index():
        index = web_root / "index.html"
        return FileResponse(
            str(index),
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    @app.get("/api/health")
    async def cockpit_health():
        return JSONResponse({"ok": True, "service": "flight_deck_web"})

    @app.post("/api/emergency")
    async def cockpit_emergency():
        try:
            from cockpit.telemetry_bridge import get_command_queue

            get_command_queue().put_nowait("EMERGENCY_FLATTEN")
            log_engine("Flight Deck web: EMERGENCY_FLATTEN queued")
            return JSONResponse({"ok": True, "status": "queued"})
        except queue.Full:
            return JSONResponse({"ok": False, "error": "command queue full"}, status_code=503)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                hot_reload = _pop_hot_reload_frame()
                if hot_reload is not None:
                    await ws.send_text(json.dumps(hot_reload, default=str))
                    continue
                payload = get_latest_telemetry()
                if not payload:
                    payload = {"ts": time.time(), "gates": {}, "epics": {}, "spread": {}}
                await ws.send_text(json.dumps(payload, default=str))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck WS client error: {type(e).__name__}: {e}")

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                lines = _read_engine_log_batch()
                frame = {
                    "type": "LOG_FRAME",
                    "ts": time.time(),
                    "lines": lines,
                }
                await ws.send_text(json.dumps(frame, default=str))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck log WS error: {type(e).__name__}: {e}")

    @app.websocket("/ws/triage")
    async def ws_triage(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                from system.supervisor_history import read_history_last_24h

                rows = read_history_last_24h(max_lines=120)
                frame = {
                    "type": "TRIAGE_FRAME",
                    "ts": time.time(),
                    "events": rows,
                }
                await ws.send_text(json.dumps(frame, default=str))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck triage WS error: {type(e).__name__}: {e}")

    return app


def start_cockpit_web_server(*, port: int = DEFAULT_COCKPIT_PORT, hz: float = 2.5) -> bool:
    """Start cockpit FastAPI + WebSocket hub in a daemon thread."""
    global _server, _thread, _loop
    with _lock:
        if _thread is not None and _thread.is_alive():
            log_engine("Flight Deck web server already running")
            return True
        _stop.clear()

        def _run() -> None:
            global _server, _loop
            import uvicorn

            config = uvicorn.Config(
                create_cockpit_app(),
                host="127.0.0.1",
                port=int(port),
                log_level="warning",
                access_log=False,
            )
            _server = uvicorn.Server(config)
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            loop = _loop
            try:
                loop.run_until_complete(_server.serve())
            finally:
                if loop is not None and not loop.is_closed():
                    loop.close()

        _thread = threading.Thread(
            target=_run, name="CockpitWebServer", daemon=True
        )
        _thread.start()

    # Wait for bind
    import socket

    deadline = time.time() + 8.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(("127.0.0.1", int(port)))
                log_engine(f"Flight Deck web cockpit live at http://127.0.0.1:{port}/")
                return True
            except OSError:
                time.sleep(0.15)
    log_engine(f"Flight Deck web server failed to bind port {port}")
    return False


def stop_cockpit_web_server() -> None:
    global _server, _thread, _loop
    _stop.set()
    with _lock:
        srv = _server
        loop = _loop
        th = _thread
        _server = None
        _loop = None
        _thread = None
    if srv is not None:
        srv.should_exit = True
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(lambda: None)
        except Exception:
            pass
    if th is not None and th.is_alive():
        th.join(timeout=3.0)
    with _latest_lock:
        _latest_payload.clear()
    with _hot_reload_lock:
        global _hot_reload_pending
        _hot_reload_pending = False


def reset_cockpit_web_for_tests() -> None:
    global _log_tail_offset
    stop_cockpit_web_server()
    _stop.clear()
    with _log_tail_lock:
        _log_tail_offset = 0
