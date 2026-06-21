"""
Isolated read-only Flight Deck — port :8787 telemetry consumer only.

Reads ``ig_agent_v30_live_state`` shared memory. Never binds :8080, never clears
ports, never mutates trading state or launches database resets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from system.engine_log import log_engine

_DEFAULT_PORT = 8787
_POLL_HZ = 2.5
_server_thread: threading.Thread | None = None
_server: Any | None = None
_loop: asyncio.AbstractEventLoop | None = None
_stop = threading.Event()
_lock = threading.Lock()


def _read_live_shm_payload() -> dict[str, Any]:
    try:
        from system.identity.shared_memory_bridge import (
            attach_shared_memory_consumer,
            reset_shared_memory_bridge,
        )

        reset_shared_memory_bridge(unlink=False)
        payload = attach_shared_memory_consumer(track="live").read_json()
        return dict(payload) if isinstance(payload, dict) else {}
    except Exception:
        return {}


_WARMED_MANIFEST_CACHE: dict[str, Any] | None = None
_WARMED_MANIFEST_MTIME: float = -1.0


def _load_warmed_manifest_cached() -> dict[str, Any] | None:
    global _WARMED_MANIFEST_CACHE, _WARMED_MANIFEST_MTIME
    try:
        from system.ml.cold_start_compiler import (
            load_warmed_alpha_manifest,
            warmed_alpha_checkpoint_path,
        )

        ckpt = warmed_alpha_checkpoint_path()
        mtime = float(ckpt.stat().st_mtime) if ckpt.is_file() else -1.0
        if mtime == _WARMED_MANIFEST_MTIME and _WARMED_MANIFEST_CACHE is not None:
            return _WARMED_MANIFEST_CACHE
        manifest = load_warmed_alpha_manifest()
        _WARMED_MANIFEST_CACHE = manifest
        _WARMED_MANIFEST_MTIME = mtime
        return manifest
    except Exception:
        return None


def _merge_warmed_alpha_telemetry(frame: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_warmed_manifest_cached()
    if not manifest:
        return frame
    try:
        from system.ml.cold_start_compiler import ml_optimization_from_manifest

        warmed = ml_optimization_from_manifest(manifest)
        ml_opt = dict(frame.get("ml_optimization") or {})
        ml_opt.update(warmed)
        frame["ml_optimization"] = ml_opt
        frame["warmed_alpha_manifest"] = {
            "checkpoint": str(manifest.get("archive_path") or ""),
            "compiled_at_utc": manifest.get("compiled_at_utc"),
            "samples_processed": manifest.get("samples_processed"),
            "evaluation_passes": manifest.get("evaluation_passes"),
            "production_confidence_floor_pct": manifest.get(
                "production_confidence_floor_pct"
            ),
        }
    except Exception:
        pass
    return frame


def _build_telemetry_frame() -> dict[str, Any]:
    payload = _read_live_shm_payload()
    hub_qs = payload.get("hub_quote_source")
    if not isinstance(hub_qs, dict):
        hub_qs = {}
    frame = {
        "ok": True,
        "service": "isolated_flight_deck",
        "read_only": True,
        "shm_segment": "ig_agent_v30_live_state",
        "track": payload.get("track", "live"),
        "api_port": payload.get("api_port", 8080),
        "updated_at_epoch": payload.get("updated_at_epoch"),
        "system_health": payload.get("system_health") or {},
        "hub_quote_source": hub_qs,
        "trailing_stops": payload.get("trailing_stops") or [],
        "ml_optimization": payload.get("ml_optimization") or {},
    }
    return _merge_warmed_alpha_telemetry(frame)


def create_isolated_cockpit_app() -> FastAPI:
    app = FastAPI(
        title="IG Agent Isolated Flight Deck",
        version="30.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/", response_model=None)
    async def root():
        from system.paths import project_root

        index = project_root() / "cockpit-web" / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {
                "ok": True,
                "service": "isolated_flight_deck",
                "read_only": True,
                "telemetry": "/api/telemetry/live-state",
                "ws": "/ws/telemetry",
            }
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        frame = _build_telemetry_frame()
        return {
            "ok": True,
            "service": "isolated_flight_deck",
            "read_only": True,
            "shm_segment": "ig_agent_v30_live_state",
            "hub_quote_source": frame.get("hub_quote_source") or {},
            "updated_at_epoch": frame.get("updated_at_epoch"),
        }

    @app.get("/api/telemetry/live-state")
    async def telemetry_live_state() -> dict[str, Any]:
        return _build_telemetry_frame()

    @app.get("/api/hub-quote-source")
    async def hub_quote_source() -> dict[str, Any]:
        frame = _build_telemetry_frame()
        return {
            "ok": True,
            "hub_quote_source": frame.get("hub_quote_source") or {},
            "updated_at_epoch": frame.get("updated_at_epoch"),
        }

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        interval = 1.0 / max(0.5, float(_POLL_HZ))
        try:
            while True:
                frame = _build_telemetry_frame()
                await ws.send_text(json.dumps(frame, default=str, allow_nan=False))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log_engine(
                f"IsolatedFlightDeck WS closed: {type(exc).__name__}: {exc}"
            )

    return app


def _run_uvicorn(port: int) -> None:
    import uvicorn

    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    config = uvicorn.Config(
        create_isolated_cockpit_app(),
        host="127.0.0.1",
        port=int(port),
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    _loop.run_until_complete(server.serve())


def start_isolated_cockpit_server(*, port: int = _DEFAULT_PORT) -> bool:
    """Bind :8787 in a dedicated daemon thread — zero coupling to Live Vanguard :8080."""
    global _server_thread
    with _lock:
        if _server_thread is not None and _server_thread.is_alive():
            log_engine(
                f"IsolatedFlightDeck: already listening on http://127.0.0.1:{port}/"
            )
            return True
        _stop.clear()
        _server_thread = threading.Thread(
            target=_run_uvicorn,
            args=(int(port),),
            name="isolated-flight-deck",
            daemon=True,
        )
        _server_thread.start()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if _port_listening(port):
            manifest = _load_warmed_manifest_cached()
            if manifest:
                log_engine(
                    "IsolatedFlightDeck: warmed-alpha manifest loaded — "
                    f"samples={manifest.get('samples_processed')} "
                    f"version={(manifest.get('weights') or {}).get('version')}"
                )
            log_engine(
                f"IsolatedFlightDeck: read-only cockpit live at "
                f"http://127.0.0.1:{port}/ (SHM ig_agent_v30_live_state)"
            )
            return True
        time.sleep(0.15)
    log_engine(f"IsolatedFlightDeck: failed to bind port {port} within timeout")
    return False


def spawn_isolated_cockpit_process(*, port: int = _DEFAULT_PORT) -> int:
    """Detached subprocess — safest 2 AM recycle (never signals :8080)."""
    import subprocess
    import sys
    from pathlib import Path

    from system.paths import project_root

    root = project_root()
    py = root / ".venv" / "bin" / "python3"
    if not py.is_file():
        py = Path(sys.executable)
    env = {
        k: v
        for k, v in {
            "HOME": __import__("os").environ.get("HOME", ""),
            "PATH": __import__("os").environ.get("PATH", ""),
            "USER": __import__("os").environ.get("USER", ""),
            "PYTHONPATH": "src",
            "IG_AGENT_ROOT": str(root),
        }.items()
        if v
    }
    proc = subprocess.Popen(
        [str(py), "-m", "api.isolated_cockpit_server", "--port", str(int(port))],
        cwd=str(root),
        env=env,
        stdout=open("/tmp/ig_agent.isolated_cockpit.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_engine(
        f"IsolatedFlightDeck: spawned detached pid={proc.pid} port={port}"
    )
    return int(proc.pid)


def _port_listening(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


def stop_isolated_cockpit_server() -> None:
    global _server_thread
    _stop.set()
    with _lock:
        _server_thread = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated read-only Flight Deck")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    args = parser.parse_args()
    _run_uvicorn(int(args.port))


if __name__ == "__main__":
    main()
