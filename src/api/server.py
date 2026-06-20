"""
FastAPI API server — lazy router bootstrap for fast Uvicorn bind.

Bootstrap routes (health + dashboard SPA shell) register at factory time so
port :8080 serves ``/`` immediately. Trading API routers mount via
``mount_deferred_routers`` after the socket is listening.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
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
    def bootstrap_api_health() -> dict[str, Any]:
        return bootstrap_health()

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
        from api.server_deferred import mount_deferred_routers

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
        else:
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
