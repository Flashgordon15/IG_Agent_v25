"""
FastAPI API server — Slice 4 Step 1 (v25 read-only state endpoints + WS stream).

All endpoints are read-only snapshots. POST /api/replay/run spawns a
subprocess trigger only — it never imports trading_loop and never writes
state files directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import threading
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.snapshot_store import get_tick, subscribe, watch_snapshot_file
from system.paths import data_dir, project_root

# ---------------------------------------------------------------------------
# File-path helpers (read-only)
# ---------------------------------------------------------------------------


def _data(filename: str) -> Path:
    return data_dir() / filename


def _watchdog_failed() -> bool:
    return _data("watchdog_failed.txt").exists()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# State router  (READ-ONLY)
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": "v25"}


@router.get("/api/state")
def api_state() -> dict[str, Any]:
    tick = get_tick()
    sig = tick.get("signal") or {}
    pts = tick.get("points") or {}
    return {
        "bid": tick.get("bid"),
        "offer": tick.get("offer"),
        "agent_state": pts.get("state", "CAUTION"),
        "points_trade": float(pts.get("last_trade") or 0),
        "points_session": float(pts.get("session") or 0),
        "points_cumulative": float(pts.get("cumulative") or 0),
        "ml_confidence": float(sig.get("confidence") or 0),
        "signal_strength": float(sig.get("confidence") or 0),
        "fitness_score": float(sig.get("fitness") or 0),
        "regime": tick.get("regime"),
        "win_rate_today": tick.get("win_rate_today"),
        "win_rate_alltime": tick.get("win_rate_20"),
        "daily_pnl_gbp": float(tick.get("daily_pnl_gbp") or 0),
        "stream_status": tick.get("stream_status", "DISCONNECTED"),
        "rest_budget": tick.get("rest_calls_min", 0),
        "spread_current": tick.get("spread"),
        "spread_normal": tick.get("spread_normal"),
        "sentiment_factor": tick.get("sentiment_factor"),
        "watchdog_failed": _watchdog_failed(),
    }


@router.get("/api/trades")
def api_trades() -> dict[str, Any]:
    tick = get_tick()
    active: list[dict[str, Any]] = list(tick.get("positions") or [])
    closed: list[dict[str, Any]] = []
    try:
        from api.dashboard_data import get_closed_trades

        for row in get_closed_trades(limit=100):
            if not row.get("deal_id"):
                continue
            if row.get("pending"):
                continue
            result = str(row.get("result") or "").upper()
            if result not in ("WIN", "LOSS", "PENDING"):
                continue
            closed.append(
                {
                    "deal_id": row["deal_id"],
                    "direction": row.get("direction"),
                    "market": row.get("market"),
                    "entry": row.get("entry"),
                    "exit": row.get("exit"),
                    "pnl_gbp": row.get("pnl_gbp"),
                    "result": result,
                    "closed_at": row.get("closed_at"),
                    "setup": row.get("setup"),
                }
            )
    except Exception:
        pass
    return {"active": active, "closed": closed}


@router.get("/api/points")
def api_points() -> dict[str, Any]:
    pts = get_tick().get("points") or {}
    return {
        "trade": float(pts.get("last_trade") or 0),
        "session": float(pts.get("session") or 0),
        "cumulative": float(pts.get("cumulative") or 0),
        "agent_state": pts.get("state", "CAUTION"),
    }


@router.get("/api/replay/summary")
def api_replay_summary() -> dict[str, Any]:
    rows = _read_jsonl(_data("replay_results.jsonl"))
    last_entry = rows[-1] if rows else {}
    replay_state = _read_json_safe(_data("replay_scheduler_state.json"))
    return {"last_result": last_entry, "replay_state": replay_state}


@router.get("/api/shadow/today")
def api_shadow_today() -> dict[str, Any]:
    today = date.today().isoformat()
    rows = [
        r
        for r in _read_jsonl(_data("shadow_log.jsonl"))
        if str(r.get("timestamp", "")).startswith(today)
    ]
    fired = [r for r in rows if r.get("would_have_fired")]
    fired_rate = round(len(fired) / len(rows), 4) if rows else 0.0
    top_3 = [
        {"setup": k, "count": v}
        for k, v in Counter(
            str(r.get("setup_key") or "unknown") for r in fired
        ).most_common(3)
    ]
    return {
        "record_count": len(rows),
        "would_have_fired_rate": fired_rate,
        "top_3_setups": top_3,
    }


@router.get("/api/learning/status")
def api_learning_status() -> dict[str, Any]:
    ml_store_rows = len(_read_jsonl(_data("ml_training_store.jsonl")))
    confirmed_trade_count = 0
    top_setups_by_win_rate: list[dict[str, Any]] = []
    try:
        from system.config_loader import ConfigLoader
        from system.paths import config_dir
        from data.learning_store import LearningStore

        cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
        store = LearningStore(str(cfg.learning_db))
        if hasattr(store, "recent_confirmed_closed_trades"):
            confirmed_trade_count = len(store.recent_confirmed_closed_trades(limit=500))
        rows = store.conn.execute(
            """
            SELECT setup_key,
                   COUNT(*) AS n,
                   ROUND(
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) * 1.0 / COUNT(*),
                       3
                   ) AS win_rate
            FROM trades
            WHERE closed_at IS NOT NULL AND setup_key IS NOT NULL
              AND (source IS NULL
                   OR (source NOT LIKE '%sim%'
                       AND source NOT LIKE '%soak%'
                       AND source NOT LIKE '%proof%'
                       AND source NOT LIKE '%replay%'
                       AND source NOT LIKE '%test%'))
            GROUP BY setup_key
            ORDER BY win_rate DESC
            LIMIT 5
            """
        ).fetchall()
        top_setups_by_win_rate = [
            {"setup_key": r[0], "count": int(r[1]), "win_rate": float(r[2])}
            for r in rows
        ]
    except Exception:
        pass
    target = 500
    progress = min(100.0, round(100 * ml_store_rows / target, 1)) if target else 0.0
    return {
        "ml_store_rows": ml_store_rows,
        "confirmed_trade_count": confirmed_trade_count,
        "top_setups_by_win_rate": top_setups_by_win_rate,
        "progress_to_500": progress,
    }


_replay_proc: subprocess.Popen | None = None  # type: ignore[type-arg]
_replay_mutex = threading.Lock()


@router.post("/api/replay/run")
def api_replay_run() -> JSONResponse:
    global _replay_proc
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

    london = ZoneInfo("Europe/London")
    now = datetime.now(london)
    minutes = now.hour * 60 + now.minute
    if minutes < 7 * 60 or minutes >= 22 * 60 + 30:
        return JSONResponse(
            {"ok": False, "error": "outside trading window 07:00\u201322:30 London"},
            status_code=409,
        )
    with _replay_mutex:
        if _replay_proc is not None and _replay_proc.poll() is None:
            return JSONResponse(
                {"ok": False, "error": "replay already running"},
                status_code=423,
            )
        script = project_root() / "scripts" / "replay_scheduler.py"
        try:
            _replay_proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(project_root()),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"launch failed: {type(exc).__name__}: {exc}"},
                status_code=500,
            )
    return JSONResponse({"ok": True, "status": "accepted"}, status_code=202)


# ---------------------------------------------------------------------------
# WebSocket /ws/stream
# ---------------------------------------------------------------------------

ws_router = APIRouter()


class _StreamHub:
    """Fan-out snapshot_store tick updates to /ws/stream WebSocket clients."""

    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub: Any | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._unsub is None:
            self._unsub = subscribe(self._on_tick_threadsafe)

    def _deliver(self, tick: dict[str, Any]) -> None:
        for q in list(self._queues.values()):
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    pass

    def _on_tick_threadsafe(self, tick: dict[str, Any]) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: self._deliver(tick))

    def register(self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[ws] = queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)


stream_hub = _StreamHub()


@ws_router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    stream_hub.register(ws, outbound)
    await outbound.put(get_tick())

    async def _reader() -> None:
        while True:
            await ws.receive_text()

    async def _writer() -> None:
        while True:
            tick = await outbound.get()
            await ws.send_json(tick)

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stream_hub.unregister(ws)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

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
        stream_hub.bind_loop(loop)
        from api import ws as _legacy_ws

        _legacy_ws.hub.bind_loop(loop)

        for hook in list(_startup_hooks):
            try:
                hook()
            except Exception as exc:
                from system.engine_log import log_engine

                log_engine(f"API startup hook failed: {type(exc).__name__}: {exc}")

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
        version="v25",
        description="Read-only state API, WebSocket stream, and static dashboard UI",
        lifespan=lifespan,
    )

    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(ws_router)

    from api import routes as _legacy_routes, ws as _legacy_ws

    app.include_router(_legacy_routes.router)
    app.include_router(_legacy_ws.router)

    dist = _dashboard_dist()
    if dist.is_dir() and (dist / "index.html").is_file():
        _mount_dashboard(app, dist)

    return app


def _mount_dashboard(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets)),
            name="dashboard-assets",
        )
    index = dist / "index.html"

    @app.get("/", include_in_schema=False)
    async def dashboard_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def dashboard_static_or_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path in ("ws", "ws/stream"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


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
