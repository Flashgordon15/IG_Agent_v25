"""
FastAPI dashboard server — API + WebSocket + static React build (Section 4.5 Steps 8/13).
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import routes, ws
from api.snapshot_store import watch_snapshot_file
from system.paths import project_root

_startup_hooks: list = []


def register_api_startup(callback) -> None:
    """Run callback after WebSocket loop is bound (start stream/trading here)."""
    _startup_hooks.append(callback)


def _dashboard_dist() -> Path:
    return project_root() / "dashboard" / "dist"


def create_app(*, watch_snapshot: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        ws.hub.bind_loop(loop)
        for hook in list(_startup_hooks):
            try:
                hook()
            except Exception as e:
                from system.engine_log import log_engine

                log_engine(f"API startup hook failed: {type(e).__name__}: {e}")
        watcher = None
        if watch_snapshot:
            watcher = asyncio.create_task(watch_snapshot_file())
        app.state.snapshot_watcher = watcher
        yield
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="IG Agent v25 API",
        version="25.1.0",
        description="Dashboard API, WebSocket ticks, and static UI at /",
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    app.include_router(ws.router)

    dist = _dashboard_dist()
    if dist.is_dir() and (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="IG Agent v25 FastAPI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(
        "api.server:app",
        host=args.host,
        port=args.port,
        reload=False,
        factory=False,
    )


if __name__ == "__main__":
    main()
