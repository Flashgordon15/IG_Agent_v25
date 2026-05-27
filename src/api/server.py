"""
FastAPI dashboard server — Section 4.5 Step 8 / 5.8 (System tab data source).

Runs on port 8080 as a separate process from the trading loop. If this process
fails, trading continues.

Read-only HTTP/WebSocket except POST /api/close/{deal_id}, which is the ONLY
endpoint that writes to trading state (manual close).

Launch:
  PYTHONPATH=src python -m api.server
  # or scripts/run_api_server.sh
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import routes, ws
from api.snapshot_store import watch_snapshot_file


def create_app(*, watch_snapshot: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        ws.hub.bind_loop(loop)
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
        version="0.1.0",
        description=(
            "Dashboard API. Read-only except POST /api/close/{deal_id} "
            "(manual position close)."
        ),
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    app.include_router(ws.router)
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
