"""
FastAPI API server — lazy router bootstrap for fast Uvicorn bind.

Bootstrap routes (health + dashboard SPA shell) register at factory time so
port :8080 serves ``/`` immediately. Trading API routers mount via
``mount_deferred_routers`` after the socket is listening.

Flight Deck cockpit (:8787) is **never** co-located with trading API lifecycle.
The isolated read-only consumer lives in ``api.isolated_cockpit_server`` and is
spawned by ``ParallelTrackSupervisor`` — it reads ``ig_agent_v30_live_state``
only and cannot clear :8080 or mutate trading state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from api.agent_control import enrich_tick_runtime
from api.snapshot_store import get_tick, subscribe
from system.paths import data_dir
from fastapi.staticfiles import StaticFiles

_startup_hooks: list = []
_DEFERRED_YIELD_ROUNDS = 8


def register_api_startup(callback) -> None:
    """Run callback in a background thread after the API port is listening."""
    _startup_hooks.append(callback)


def _run_startup_hooks() -> None:
    from system.engine_log import log_engine

    for hook in list(_startup_hooks):
        try:
            hook()
        except Exception as exc:
            log_engine(f"API startup hook failed: {type(exc).__name__}: {exc}")


def _register_bootstrap_routes(app: FastAPI) -> None:
    """Minimal routes for watchdog + boot splash — no trading imports."""
    from api.auth import register_auth_login_route

    register_auth_login_route(app)

    @app.get("/health", include_in_schema=False)
    def bootstrap_health() -> dict[str, Any]:
        from system.app_identity import APP_VERSION_LABEL
        from system.boot_metrics import get_boot_metrics

        boot = get_boot_metrics()
        stage = str(boot.get("stage") or "booting")
        warming = stage == "warming" or bool(boot.get("warming"))
        return {
            "status": "warming" if warming else "ok",
            "version": APP_VERSION_LABEL,
            "bootstrapping": not bool(boot.get("ready")),
            "ready": bool(boot.get("ready")),
            "warming": warming,
            "progress": int(boot.get("percent") or 0),
        }

    @app.get("/api/health", include_in_schema=False)
    def bootstrap_api_health() -> JSONResponse:
        from datetime import datetime, timezone

        from api.gate_health_matrix import build_gate_health_response
        from api.snapshot_store import snapshot_age_s_fast
        from fastapi.responses import JSONResponse

        code, body = build_gate_health_response(include_extended=True)
        body["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        body["snapshot_age_s"] = snapshot_age_s_fast()
        return JSONResponse(status_code=code, content=body)

    @app.get("/api/startup/status", include_in_schema=False)
    def bootstrap_startup_status() -> dict[str, Any]:
        from system.boot_metrics import get_boot_metrics
        from system.system_state import get_system_state

        boot_metrics = get_boot_metrics()
        system_state = get_system_state().snapshot()
        phases: list[Any] = []
        try:
            from system.startup_tracker import get_status

            phases = list(get_status().get("phases") or [])
        except Exception:
            pass
        from api.restriction_diagnostics import enrich_restrictions_payload

        ready = bool(system_state.get("ready")) and bool(boot_metrics.get("ready"))
        if str(boot_metrics.get("stage") or "") == "warming":
            ready = False

        return enrich_restrictions_payload(
            {
                "boot_metrics": boot_metrics,
                "system_state": system_state,
                "ready": ready,
                "background_verify": system_state.get("background_verify") or {},
                "phases": phases,
            }
        )

    @app.get("/api/ui/status", include_in_schema=False)
    def bootstrap_ui_status() -> dict[str, Any]:
        dist = getattr(app.state, "dashboard_dist", None)
        return {
            "ui_ready": bool(getattr(app.state, "dashboard_static_mounted", False)),
            "dist": str(dist) if dist else None,
        }


_DASHBOARD_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


_BOOT_SPLASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>IG Agent — Starting</title>
  <style>
    body{font-family:system-ui,sans-serif;background:#0b1220;color:#e8eef7;
         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .card{max-width:32rem;padding:2rem;border:1px solid #243049;border-radius:12px;
          background:#121a2b;text-align:center}
    h1{font-size:1.25rem;margin:0 0 .75rem}
    p{margin:.35rem 0;color:#9fb0cc;font-size:.95rem}
  </style>
</head>
<body>
  <div class="card">
    <h1>IG Agent dashboard loading…</h1>
    <p id="status">API online — waiting for built dashboard assets.</p>
    <p>Run: <code>cd dashboard && npm install && npm run build</code></p>
  </div>
  <script>
    (function poll(){
      fetch('/api/startup/status').then(r=>r.json()).then(d=>{
        const pct = d.system_state && d.system_state.percent;
        document.getElementById('status').textContent =
          'Boot ' + (pct != null ? pct : 0) + '% — reloading when dist is ready…';
        if (d.ready) location.reload();
      }).catch(()=>{});
      setTimeout(poll, 1500);
    })();
  </script>
</body>
</html>"""


def _ensure_ig_agent_root_env() -> None:
    """Ensure dashboard path resolution works before first request."""
    if os.environ.get("IG_AGENT_ROOT", "").strip():
        return
    repo = Path(__file__).resolve().parents[2]
    if (repo / "src" / "main.py").is_file():
        os.environ["IG_AGENT_ROOT"] = str(repo)
        return
    try:
        from system.paths import project_root

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
    except Exception:
        pass


def _unified_engine_active() -> bool:
    return os.environ.get("IG_UNIFIED_ENGINE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def resolve_unified_fulfillment_template() -> Path | None:
    """Lightweight static dashboard for headless unified engine."""
    _ensure_ig_agent_root_env()
    candidates: list[Path] = []
    env_root = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).resolve() / "src" / "templates" / "index.html")
    repo_from_file = Path(__file__).resolve().parents[1] / "templates" / "index.html"
    candidates.append(repo_from_file)
    try:
        from system.paths import project_root

        candidates.append(project_root() / "src" / "templates" / "index.html")
    except Exception:
        pass
    seen: set[Path] = set()
    for path in candidates:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            return path
    return None


def resolve_dashboard_dist() -> Path | None:
    """
    Locate ``dashboard/dist`` using absolute paths (launcher / terminal / cwd safe).

    Resolution order:
    1. ``$IG_AGENT_ROOT/dashboard/dist`` (desktop launcher, launchd)
    2. Repo root derived from this file — ``src/api/server.py`` → parents[2]
    3. ``system.paths.project_root()`` (bundle / frozen exe aware)
    """
    candidates: list[Path] = []
    _ensure_ig_agent_root_env()
    env_root = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env_root:
        candidates.append((Path(env_root).resolve() / "dashboard" / "dist"))
    repo_from_file = Path(__file__).resolve().parents[2]
    candidates.append((repo_from_file / "dashboard" / "dist"))
    candidates.append((Path.cwd().resolve() / "dashboard" / "dist"))
    try:
        from system.paths import project_root

        pr = (project_root() / "dashboard" / "dist").resolve()
        if pr not in candidates:
            candidates.append(pr)
    except Exception:
        pass

    seen: set[Path] = set()
    for dist in candidates:
        dist = dist.resolve()
        if dist in seen:
            continue
        seen.add(dist)
        if (dist / "index.html").is_file():
            return dist
    return None


def _mount_dashboard_static(app: FastAPI) -> None:
    """Register ``/``, ``/assets/*``, and favicon at factory time (fast path)."""
    if getattr(app.state, "dashboard_static_mounted", False):
        return

    _ensure_ig_agent_root_env()

    if _unified_engine_active():
        unified_tpl = resolve_unified_fulfillment_template()
        if unified_tpl is not None:
            @app.get("/", include_in_schema=False)
            async def unified_fulfillment_root() -> FileResponse:
                return FileResponse(unified_tpl, headers=_DASHBOARD_NO_CACHE)

            app.state.dashboard_dist = None
            try:
                from system.engine_log import log_engine

                log_engine(f"API: unified fulfillment template mounted from {unified_tpl}")
            except Exception:
                pass
            app.state.dashboard_static_mounted = True
            return

    dist = resolve_dashboard_dist()
    index: Path | None = None
    if dist is not None:
        index = dist / "index.html"

    if dist is not None and index is not None and index.is_file():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets)),
                name="dashboard-assets",
            )

        favicon = dist / "favicon.svg"

        @app.get("/", include_in_schema=False)
        async def dashboard_root() -> FileResponse:
            return FileResponse(index, headers=_DASHBOARD_NO_CACHE)

        if favicon.is_file():

            @app.get("/favicon.svg", include_in_schema=False)
            async def dashboard_favicon() -> FileResponse:
                return FileResponse(favicon, headers=_DASHBOARD_NO_CACHE)

        app.state.dashboard_dist = dist
        try:
            from system.engine_log import log_engine

            log_engine(f"API: dashboard static shell mounted from {dist}")
        except Exception:
            pass
    else:
        searched = [
            str((Path(os.environ.get("IG_AGENT_ROOT", "")).resolve() / "dashboard" / "dist"))
            if os.environ.get("IG_AGENT_ROOT")
            else "",
            str(Path(__file__).resolve().parents[2] / "dashboard" / "dist"),
        ]
        try:
            from system.engine_log import log_engine

            log_engine(
                "API: dashboard/dist/index.html not found — serving boot splash at /. "
                f"Searched: {', '.join(p for p in searched if p)}"
            )
        except Exception:
            pass

        @app.get("/", include_in_schema=False)
        async def dashboard_boot_splash() -> HTMLResponse:
            return HTMLResponse(_BOOT_SPLASH_HTML, headers=_DASHBOARD_NO_CACHE)

    app.state.dashboard_static_mounted = True


def _cors_allow_origins() -> list[str]:
    origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:9090",
        "http://127.0.0.1:9090",
        "http://localhost:9191",
        "http://127.0.0.1:9191",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    try:
        from system.node_profile import get_node_profile

        profile = get_node_profile()
        origins.extend(
            [
                profile.dashboard_url.rstrip("/"),
                profile.cockpit_url.rstrip("/"),
            ]
        )
    except Exception:
        pass
    return list(dict.fromkeys(origins))


def _register_api_middleware(app: FastAPI) -> None:
    """Register auth/CORS at factory time — Starlette forbids add_middleware after start."""
    from fastapi.middleware.cors import CORSMiddleware

    from api.auth_middleware import AdminAuthMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AdminAuthMiddleware)


async def _yield_event_loop(rounds: int = _DEFERRED_YIELD_ROUNDS) -> None:
    """Cooperative scheduler drain before heavy sync/thread boot work."""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _start_snapshot_watcher(watch_snapshot: bool) -> asyncio.Task | None:
    if not watch_snapshot:
        return None
    from api.snapshot_store import watch_snapshot_file

    return asyncio.create_task(watch_snapshot_file())


async def _cancel_snapshot_watcher(watcher: asyncio.Task | None) -> None:
    if watcher is None:
        return
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass


async def _run_boot_pipeline(
    app: FastAPI,
    *,
    watch_snapshot: bool,
    shutdown: asyncio.Event,
    mount_done: asyncio.Event,
) -> None:
    watcher: asyncio.Task | None = None
    await _yield_event_loop()
    try:
        from system.boot_coordinator import boot_lifespan

        async with boot_lifespan(app):
            mount_done.set()
            watcher = await _start_snapshot_watcher(watch_snapshot)
            app.state.snapshot_watcher = watcher
            await shutdown.wait()
    finally:
        await _cancel_snapshot_watcher(watcher)


async def _run_legacy_deferred_mount(
    app: FastAPI,
    loop: asyncio.AbstractEventLoop,
    *,
    watch_snapshot: bool,
    shutdown: asyncio.Event,
    mount_done: asyncio.Event,
) -> None:
    watcher: asyncio.Task | None = None
    await _yield_event_loop()
    try:
        await asyncio.to_thread(mount_deferred_routers, app, loop)
        mount_done.set()
        threading.Thread(
            target=_run_startup_hooks,
            name="api-startup-hooks",
            daemon=True,
        ).start()
        watcher = await _start_snapshot_watcher(watch_snapshot)
        app.state.snapshot_watcher = watcher
        await shutdown.wait()
    finally:
        await _cancel_snapshot_watcher(watcher)


def create_app(
    *,
    watch_snapshot: bool = True,
    use_boot_pipeline: bool = True,
    boot_context: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        mount_done = asyncio.Event()
        app.state._api_shutdown = shutdown
        app.state._mount_done = mount_done

        async def _deferred_worker() -> None:
            if use_boot_pipeline:
                await _run_boot_pipeline(
                    app,
                    watch_snapshot=watch_snapshot,
                    shutdown=shutdown,
                    mount_done=mount_done,
                )
            else:
                await _run_legacy_deferred_mount(
                    app,
                    loop,
                    watch_snapshot=watch_snapshot,
                    shutdown=shutdown,
                    mount_done=mount_done,
                )

        # Detached from lifespan startup — Uvicorn binds at ``yield`` below.
        deferred_task = asyncio.create_task(
            _deferred_worker(), name="api-deferred-startup"
        )

        if os.environ.get("IG_AGENT_PYTEST") == "1":
            await mount_done.wait()
        elif os.environ.get("IG_TEST_HARNESS") != "1":
            try:
                from system.manual_kill_monitor import start_manual_kill_monitor

                start_manual_kill_monitor()
            except Exception:
                pass

        try:
            yield
        finally:
            shutdown.set()
            deferred_task.cancel()
            try:
                await deferred_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="IG Agent v25 API",
        version="v25",
        description="Read-only state API, WebSocket stream, and static dashboard UI",
        lifespan=lifespan,
    )
    if boot_context is not None:
        app.state.boot_context = boot_context

    # Fast path — health + dashboard shell available the instant :8080 binds.
    # Trading API routers register in mount_deferred_routers (post-bind), not here.
    _register_api_middleware(app)
    _register_bootstrap_routes(app)
    _mount_dashboard_static(app)
    return app



# --- Monolithic deferred API surface (formerly server_deferred.py) ---

_router_mounted = False

_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


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


router = APIRouter()


@router.get("/api/live-state")
def api_live_state() -> dict[str, Any]:
    """Read dual-track telemetry — live + mock SHM segments with GUI prefixes."""
    from system.identity.shared_memory_bridge import read_dual_track_telemetry_envelope

    return read_dual_track_telemetry_envelope()


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
        "fitness_factors": sig.get("fitness_factors") or {},
        "signal_threshold": float(sig.get("threshold") or 0),
        "config_signal_threshold": float(sig.get("config_signal_threshold") or 0),
        "min_size_threshold": float(sig.get("min_size_threshold") or 0),
        "points_confidence_floor": float(sig.get("points_confidence_floor") or 0),
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
    from system.replay_scheduler_state import load_replay_scheduler_state

    rows = _read_jsonl(_data("replay_results.jsonl"))
    last_entry = rows[-1] if rows else {}
    replay_state = load_replay_scheduler_state()
    return {"last_result": last_entry, "replay_state": replay_state}


@router.get("/api/shadow/today")
def api_shadow_today() -> dict[str, Any]:
    from api.intelligence_data import shadow_today as _shadow_today

    return _shadow_today()


@router.get("/api/learning/status")
def api_learning_status() -> dict[str, Any]:
    from api.intelligence_data import learning_status as _learning_status

    return _learning_status()


@router.get("/api/learning/status_legacy")
def api_learning_status_legacy() -> dict[str, Any]:
    ml_store_rows = len(_read_jsonl(_data("ml_training_store.jsonl")))
    confirmed_trade_count = 0
    top_setups_by_win_rate: list[dict[str, Any]] = []
    try:
        from data.learning_store import LearningStore
        from system.config_loader import ConfigLoader
        from system.paths import config_dir

        from system.config_loader import load_active_config

        cfg = load_active_config(validate=False)
        store = LearningStore(str(cfg.learning_db))
        if hasattr(store, "recent_confirmed_closed_trades"):
            confirmed_trade_count = len(store.recent_confirmed_closed_trades(limit=500))
        rows = store.conn.execute(
            """
            SELECT setup_key, COUNT(*) AS n,
                   ROUND(SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 3) AS win_rate
            FROM trades WHERE closed_at IS NOT NULL AND setup_key IS NOT NULL
            GROUP BY setup_key ORDER BY win_rate DESC LIMIT 5
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


_replay_mutex = threading.Lock()


@router.post("/api/replay/run")
def api_replay_run() -> Any:
    from fastapi.responses import JSONResponse

    from system.replay_scheduler_runner import in_replay_api_window, run_replay_pipeline
    from system.replay_scheduler_state import load_replay_scheduler_state

    if not in_replay_api_window():
        return JSONResponse(
            {"ok": False, "error": "outside trading window 07:00\u201322:30 London"},
            status_code=409,
        )
    state = load_replay_scheduler_state()
    if str(state.get("status") or "") == "running":
        return JSONResponse(
            {"ok": False, "error": "replay already running"},
            status_code=423,
        )
    with _replay_mutex:

        def _run() -> None:
            try:
                run_replay_pipeline(scheduled=False)
            except Exception as exc:
                from system.engine_log import log_engine

                log_engine(f"api replay run failed: {type(exc).__name__}: {exc}")

        try:
            live = threading.active_count()
            if live > 400:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            f"thread count high ({live}) — restart agent to free threads, "
                            "then retry replay"
                        ),
                    },
                    status_code=503,
                )
            threading.Thread(target=_run, name="replay-manual", daemon=True).start()
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"launch failed: {type(exc).__name__}: {exc}"},
                status_code=500,
            )
    return JSONResponse({"ok": True, "status": "accepted"}, status_code=202)


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
        enriched = enrich_tick_runtime(tick)
        for q in list(self._queues.values()):
            try:
                q.put_nowait(enriched)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(enriched)
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


class _SharedMemoryTelemetryHub:
    """
    Decoupled API consumer — polls both track SHM segments, broadcasts enveloped JSON.

    Network failures stop at this process barrier; trading loops never observe
    WebSocket disconnects or browser reloads.
    """

    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_live_epoch: float = 0.0
        self._last_shadow_epoch: float = 0.0

    def attach_shared_memory(self) -> None:
        from system.identity.shared_memory_bridge import attach_shared_memory_consumer

        attach_shared_memory_consumer(track="live")
        attach_shared_memory_consumer(track="shadow")

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None:
            return
        self.attach_shared_memory()
        self._loop = loop
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="shared-memory-telemetry-poller",
            daemon=True,
        )
        self._thread.start()

    def _poll_loop(self) -> None:
        from system.identity.shared_memory_bridge import (
            read_dual_track_telemetry_envelope,
            read_track_state_payload,
        )

        while not self._stop.is_set():
            try:
                live_payload = read_track_state_payload("live")
                shadow_payload = read_track_state_payload("shadow")
                live_epoch = float(live_payload.get("updated_at_epoch") or 0.0)
                shadow_epoch = float(shadow_payload.get("updated_at_epoch") or 0.0)
                if (
                    live_epoch > self._last_live_epoch
                    or shadow_epoch > self._last_shadow_epoch
                ):
                    self._last_live_epoch = live_epoch
                    self._last_shadow_epoch = shadow_epoch
                    envelope = read_dual_track_telemetry_envelope()
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(
                            lambda p=envelope: self._deliver(p)
                        )
            except Exception:
                pass
            self._stop.wait(timeout=0.25)

    def _deliver(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws, q in list(self._queues.items()):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._queues.pop(ws, None)

    def register(self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[ws] = queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)


telemetry_stream_hub = _SharedMemoryTelemetryHub()
live_state_hub = telemetry_stream_hub


async def _serve_telemetry_stream(ws: WebSocket) -> None:
    """Shared handler — dual-track envelope with [LIVE-TRACK] / [MOCK-TRACK] prefixes."""
    from system.identity.shared_memory_bridge import read_dual_track_telemetry_envelope

    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
    telemetry_stream_hub.register(ws, outbound)
    await outbound.put(read_dual_track_telemetry_envelope())

    async def _reader() -> None:
        while True:
            await ws.receive_text()

    async def _writer() -> None:
        while True:
            payload = await outbound.get()
            await ws.send_json(payload)

    try:
        await asyncio.gather(_reader(), _writer())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        telemetry_stream_hub.unregister(ws)


@ws_router.websocket("/api/telemetry/stream")
async def ws_api_telemetry_stream(ws: WebSocket) -> None:
    """Institutional telemetry stream — reads shared RAM, same JSON as ``/api/live-state``."""
    await _serve_telemetry_stream(ws)


@ws_router.websocket("/ws/live-state")
async def ws_live_state(ws: WebSocket) -> None:
    """Backward-compatible alias — identical payload to ``/api/telemetry/stream``."""
    await _serve_telemetry_stream(ws)


@ws_router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    await ws.accept()
    outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    stream_hub.register(ws, outbound)
    await outbound.put(enrich_tick_runtime(get_tick()))

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


def _dashboard_dist() -> Path:
    dist = resolve_dashboard_dist()
    if dist is not None:
        return dist
    return (Path(__file__).resolve().parents[2] / "dashboard" / "dist").resolve()


def _mount_dashboard_spa_fallback(app: FastAPI, dist: Path) -> None:
    """SPA deep-link fallback — must run after all ``/api/*`` routers are registered."""
    if getattr(app.state, "dashboard_spa_fallback_mounted", False):
        return

    index = dist / "index.html"
    if not index.is_file():
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    async def dashboard_static_or_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path in ("ws", "ws/stream", "ws/live-state"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers=_NO_CACHE)

    app.state.dashboard_spa_fallback_mounted = True


def register_deferred_route_tables(app: FastAPI) -> None:
    """Register trading API + websocket routes at factory time (before Uvicorn start)."""
    if getattr(app.state, "deferred_routes_registered", False):
        return

    from api import routes as _legacy_routes
    from api import ws as _legacy_ws

    app.include_router(router)
    app.include_router(ws_router)
    app.include_router(_legacy_routes.router)
    app.include_router(_legacy_ws.router)

    dist = getattr(app.state, "dashboard_dist", None) or _dashboard_dist()
    if (dist / "index.html").is_file():
        _mount_dashboard_spa_fallback(app, dist)

    app.state.deferred_routes_registered = True


def mount_deferred_routers(app: FastAPI, loop: asyncio.AbstractEventLoop) -> None:
    """Post-bind: register trading routes, bind websocket loop, start health cache."""
    global _router_mounted
    if _router_mounted or getattr(app.state, "deferred_routers_mounted", False):
        return

    register_deferred_route_tables(app)

    stream_hub.bind_loop(loop)
    telemetry_stream_hub.bind_loop(loop)
    from api import ws as _legacy_ws

    _legacy_ws.hub.bind_loop(loop)

    try:
        from api.agent_health import start_health_cache_refresher

        start_health_cache_refresher()
    except Exception:
        pass

    _router_mounted = True
    app.state.deferred_routers_mounted = True

    from system.engine_log import log_engine

    log_engine("API: deferred trading routers mounted")


def isolated_cockpit_policy_summary() -> dict[str, str]:
    """Document native Flight Deck isolation — no shell port-clearing scripts."""
    return {
        "cockpit_port": "8787",
        "cockpit_module": "api.isolated_cockpit_server",
        "shm_segment": "ig_agent_v30_live_state",
        "mode": "read_only",
        "live_vanguard_port": "8080",
        "policy": "8787 recycle never signals 8080",
    }


def main() -> None:
    import uvicorn

    from system.boot.preflight_helpers import resolve_api_port

    parser = argparse.ArgumentParser(description="IG Agent v25 FastAPI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=resolve_api_port())
    args = parser.parse_args()
    uvicorn.run(
        "api.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
