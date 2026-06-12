"""
IG Agent v29 entry point — launchd / manual start.

Preflight: emergency lock, config validation, demo guard, instance lock, credentials.
Runtime: trading loop (background) + FastAPI on :8080 (foreground).
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from api.agent_control import register_trading_loop
from api.server import create_app, register_api_startup
from system.app_identity import APP_DISPLAY_NAME, APP_VERSION_LABEL
from system.config import Config
from system.config_loader import ConfigLoader
from system.config_validator import (
    apply_config_defaults,
    emergency_stop_lock_present,
    validate_config,
)
from system.credentials_holder import bootstrap_credentials, get_credentials_holder
from system.credentials_loader import try_load_credentials
from system.engine_log import log_engine
from system.instance_lock import (
    acquire_instance_lock,
    release_instance_lock,
)
from system.instance_lock import (
    lock_path as instance_lock_path,
)
from system.paths import logs_dir, project_root

try:
    from system.startup_tracker import mark as _startup_mark
except Exception:

    def _startup_mark(phase_id: str, note: str | None = None) -> None:  # type: ignore[misc]
        pass


EXIT_OK = 0
EXIT_LOCK = 2
EXIT_CONFIG = 3
EXIT_INSTANCE = 4

_SESSION_REFRESH_INTERVAL_SEC = 45 * 60  # 45 minutes
_LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — rotate shell-written logs
_LOG_KEEP_BACKUPS = 3


def _rotate_oversized_logs() -> None:
    """Rotate any shell-written log files that exceed the size cap.

    Python logging uses RotatingFileHandler already; this handles files written
    by shell redirects (launcher.log, ig_agent.log) that bypass Python's handler.
    """
    from pathlib import Path

    from system.paths import logs_dir

    log_dir = logs_dir()
    for log_path in log_dir.glob("*.log"):
        try:
            if log_path.stat().st_size <= _LOG_ROTATE_MAX_BYTES:
                continue
            # Rotate: .log → .log.1 → .log.2 etc., drop oldest
            for i in range(_LOG_KEEP_BACKUPS - 1, 0, -1):
                src = Path(f"{log_path}.{i}")
                dst = Path(f"{log_path}.{i + 1}")
                if src.exists():
                    src.rename(dst)
            log_path.rename(Path(f"{log_path}.1"))
            log_path.touch()  # create fresh empty file
        except Exception:
            pass


def _start_session_refresh_watchdog(rest_client: Any) -> None:
    """Background thread that proactively refreshes the IG session every 45 minutes.

    Without this, a long-running Lightstreamer session (no REST calls) can let
    the session token expire silently, causing an auth failure on the next trade.
    """
    if rest_client is None:
        return

    def _refresh_loop() -> None:
        while True:
            time.sleep(_SESSION_REFRESH_INTERVAL_SEC)
            try:
                refreshed = rest_client.proactive_refresh_if_needed()
                if not refreshed:
                    # Force a lightweight REST call to keep the session alive
                    try:
                        rest_client.ensure_session()
                        log_engine("IG session keep-alive: session verified")
                    except Exception as e:
                        log_engine(
                            f"IG session keep-alive failed: {type(e).__name__}: {e}"
                        )
                else:
                    log_engine(
                        "IG session keep-alive: proactive token refresh completed"
                    )
            except Exception as e:
                log_engine(
                    f"IG session refresh watchdog error: {type(e).__name__}: {e}"
                )

    t = threading.Thread(target=_refresh_loop, name="ig-session-refresh", daemon=True)
    t.start()
    log_engine(
        f"IG session refresh watchdog started (interval {_SESSION_REFRESH_INTERVAL_SEC // 60}m)"
    )


_BROWSER_DELAY_SEC = 3.0
_API_HOST = "127.0.0.1"
_API_PORT = 8080
_DASHBOARD_URL = "http://localhost:8080/"


def _is_benign_startup_lock_failure(message: str) -> bool:
    """
    Detect lock outcomes that should not count as watchdog startup failures.

    These happen during normal duplicate-launch paths while an existing agent runs.
    """
    txt = str(message or "").strip().lower()
    if not txt:
        return False
    benign_markers = (
        "another ig agent instance is running",
        "already running",
        "duplicate",
    )
    return any(marker in txt for marker in benign_markers)


def check_port_available(port: int) -> bool:
    """Return True if nothing is accepting TCP connections on 127.0.0.1:port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    result = s.connect_ex((_API_HOST, port))
    s.close()
    return result != 0


def _port_in_use_banner(port: int) -> str:
    return (
        "\n"
        "================================================================================\n"
        f"{APP_DISPLAY_NAME}: port {port} is already in use\n"
        "\n"
        f"Another process is listening on http://{_API_HOST}:{port}/\n"
        "\n"
        "To find the process:\n"
        f"  lsof -i :{port} -sTCP:LISTEN\n"
        "\n"
        "To stop it (replace PID with the number from lsof):\n"
        "  kill <PID>\n"
        "\n"
        "Or quit the other IG Agent / API server from the dock or Activity Monitor.\n"
        "================================================================================\n"
    )


def _config_path() -> Path:
    from system.config_loader import _primary_config_path

    return _primary_config_path()


def load_raw_config_dict() -> dict[str, Any]:
    """Load fully merged config (respects v29 → v25 $extends chain)."""
    return ConfigLoader(_config_path()).load_config(validate=False).as_dict()


def merge_credentials_for_validation(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay IG credentials from credentials.json for validator critical keys."""
    merged = dict(data)
    status = try_load_credentials()
    if status.credentials is not None:
        c = status.credentials
        merged.update(
            {
                "ig_username": c.ig_username,
                "ig_password": c.ig_password,
                "ig_api_key": c.ig_api_key,
                "ig_account_id": c.ig_account_id,
                "account_id": c.ig_account_id,
            }
        )
    return merged


def _clear_pycache() -> None:
    """Remove all __pycache__ dirs under src/ to force fresh bytecode on launch."""
    import shutil

    src_root = Path(__file__).parent
    cleared = 0
    for cache_dir in src_root.rglob("__pycache__"):
        try:
            shutil.rmtree(cache_dir)
            cleared += 1
        except Exception:
            pass
    log_engine(
        f"startup: cleared {cleared} __pycache__ dirs — fresh bytecode guaranteed"
    )


def _init_telegram_from_config() -> None:
    """Configure Telegram as early as possible so failure paths can alert."""
    try:
        raw = load_raw_config_dict()
        from system.config import Config
        from system.config_loader import _sync_operating_mode_from_credentials
        from system.config_validator import apply_config_defaults
        from system.telegram_notifier import configure_telegram

        merged = apply_config_defaults(raw)
        _sync_operating_mode_from_credentials(merged)
        configure_telegram(Config(_data=merged))
    except Exception as e:
        log_engine(f"telegram early init failed: {type(e).__name__}: {e}")


def _run_deployment_verification() -> None:
    """Run deployment health checks — abort startup if any fail."""
    if os.environ.get("IG_AGENT_SKIP_DEPLOY_CHECK") == "1":
        log_engine(
            "Deployment verification skipped (IG_AGENT_SKIP_DEPLOY_CHECK=1 — watchdog restart)"
        )
        _startup_mark("deploy_check", note="skipped watchdog restart")
        return
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_deployment_verified.py",
            "-q",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)},
    )
    if result.returncode != 0:
        log_engine(
            f"DEPLOYMENT VERIFICATION FAILED — agent will not start trading:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert("Startup BLOCKED — deployment check failed")
        except Exception:
            pass
        raise SystemExit(
            "Deployment verification failed. Fix the issues above before launching."
        )
    last_line = (
        result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "ok"
    )
    log_engine(f"Deployment verification passed ({last_line})")
    _startup_mark("deploy_check", note="all checks passed")


def _pre_startup_cleanup() -> None:
    """Kill any stale agent processes and release resources before acquiring a new lock.

    Runs every time the agent starts so a previous crash, force-quit, or silent
    background session never blocks the next launch.
    """
    _clear_pycache()
    from system.shutdown_cleanup import clear_manual_stop

    clear_manual_stop()
    _init_telegram_from_config()
    _run_deployment_verification()

    import os

    my_pid = os.getpid()
    lock_file = instance_lock_path()
    killed_pids: list[int] = []

    # 1. Find and SIGTERM any other agent processes
    from system.shutdown_cleanup import kill_other_agent_processes

    killed_pids = kill_other_agent_processes(
        exclude_pid=my_pid,
        sigkill_survivors=True,
        wait_sec=5.0,
        log_label="pre-startup",
    )

    # 2b. Orphan watchdog from a prior session can race this startup — stop it first.
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if not launchd_watchdog_active():
            from api.agent_health import stop_watchdog

            stop_watchdog(preserve_launchd=False)
            log_engine("pre-startup: cleared standalone watchdog from prior session")
    except Exception as e:
        log_engine(f"pre-startup: watchdog cleanup error (ignored): {e}")

    # 3. Remove stale lock
    try:
        if lock_file.exists():
            lock_file.unlink()
            log_engine("pre-startup: removed stale instance lock")
    except Exception as e:
        log_engine(f"pre-startup: could not remove lock: {e}")

    # 4. Kill any process still bound to port 8080 (lsof catches zombie workers
    #    that pgrep may have missed, e.g. uvicorn sub-processes).
    _force_cleanup_port(_API_PORT)

    # 5. Wait for port 8080 to be free (previous server may still be tearing down)
    import socket as _socket

    _port_free = False
    for _ in range(10):  # up to 3 s
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", _API_PORT)) != 0:
                _port_free = True
                break
        time.sleep(0.3)
    if not _port_free:
        log_engine(
            "pre-startup: port 8080 still in use after cleanup — proceeding anyway"
        )

    # 6. Clear any stale in-flight / pending-order state left by the previous
    #    session.  These are in-memory-only dicts that survive process death via
    #    runtime_state.json.  Clearing them here means a fresh session always
    #    starts from a clean state; broker reconciliation re-establishes the
    #    correct view within seconds of the first position-sync tick.
    try:
        from execution.entry_inflight import recover_startup_inflight_state
        from execution.pending_order_reconcile import recover_pending_state_for_startup

        cleared_pending = recover_pending_state_for_startup()
        cleared_inflight = recover_startup_inflight_state()
        if cleared_pending or cleared_inflight:
            log_engine(
                f"pre-startup: cleared {cleared_pending} stale pending order(s) "
                f"and {cleared_inflight} in-flight entry/ies from previous session"
            )
    except Exception as e:
        log_engine(f"pre-startup: inflight/pending clear failed (ignored): {e}")

    # 7. Mark startup phase (visible in splash screen)
    note = (
        f"killed {len(killed_pids)} previous session(s)"
        if killed_pids
        else "no previous session running"
    )
    _startup_mark("session_cleanup", note)
    log_engine(f"pre-startup: cleanup complete — {note}")


def _ensure_watchdog_running() -> None:
    """Start scripts/watchdog.sh when absent — skip if launchd already owns supervision."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            log_engine(
                "startup: launchd watchdog active — skipping manual watchdog spawn"
            )
            return
    except Exception:
        pass

    try:
        from api.agent_health import _watchdog_active

        if _watchdog_active():
            log_engine("startup: watchdog already running")
            return
    except Exception:
        pass

    wd = project_root() / "scripts" / "watchdog.sh"
    if not wd.is_file():
        log_engine(f"startup: watchdog script missing ({wd})")
        return
    if not os.access(wd, os.X_OK):
        log_engine("startup: watchdog script not executable")
        return

    log_path = logs_dir() / "watchdog.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_fh:
            subprocess.Popen(
                ["bash", str(wd)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(project_root()),
            )
        log_engine("startup: watchdog started")
    except Exception as e:
        log_engine(f"startup: watchdog start failed: {type(e).__name__}: {e}")


def _force_cleanup_port(port: int = 8080) -> None:
    """Kill any process listening on *port* (other than self) and remove the lock.

    Uses ``lsof -ti :<port>`` which catches zombie uvicorn workers that
    ``pgrep -f src/main.py`` misses.  Safe to call at startup and on exit.
    """
    own_pid = os.getpid()
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", f":{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            if pid == own_pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                log_engine(f"cleanup: SIGKILL PID {pid} on port {port}")
            except ProcessLookupError:
                pass
            except Exception as e:
                log_engine(f"cleanup: could not kill PID {pid}: {e}")
    except Exception:
        pass
    instance_lock_path().unlink(missing_ok=True)


def run_preflight() -> int:
    """Steps 1–4. Returns exit code (0 = continue)."""
    if emergency_stop_lock_present():
        print(
            f"{APP_DISPLAY_NAME}: emergency_stop.lock present — delete it to restart.",
            file=sys.stderr,
        )
        return EXIT_LOCK

    try:
        raw = load_raw_config_dict()
    except Exception as e:
        print(f"{APP_DISPLAY_NAME}: config load failed: {e}", file=sys.stderr)
        return EXIT_CONFIG

    validation_cfg = merge_credentials_for_validation(raw)
    valid, messages = validate_config(validation_cfg)
    if not valid:
        for line in messages:
            if line.startswith("ERROR:"):
                print(f"{APP_DISPLAY_NAME}: {line}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        from system.demo_guard import validate_demo_only_startup

        demo_ok, demo_msg = validate_demo_only_startup(validation_cfg)
        if not demo_ok:
            print(f"{APP_DISPLAY_NAME}: {demo_msg}", file=sys.stderr)
            return EXIT_CONFIG
        log_engine(f"preflight: {demo_msg}")
    except Exception as e:
        log_engine(f"preflight: demo guard error (continuing): {type(e).__name__}: {e}")

    ok, msg = acquire_instance_lock()
    if not ok:
        try:
            if not _is_benign_startup_lock_failure(msg):
                from system.watchdog_banner import record_startup_failure

                record_startup_failure(msg)
        except Exception:
            pass
        print(f"{APP_DISPLAY_NAME}: {msg}", file=sys.stderr)
        return EXIT_INSTANCE
    try:
        from system.watchdog_banner import record_startup_success

        record_startup_success()
    except Exception:
        pass

    holder = bootstrap_credentials()
    if holder.credentials:
        log_engine(f"credentials bootstrap: loaded ({holder.credentials.account_type})")
    else:
        log_engine(f"credentials bootstrap: not ready — {holder.status.error}")

    _startup_mark("preflight")
    return EXIT_OK


def _open_browser_delayed(url: str, delay: float = _BROWSER_DELAY_SEC) -> None:
    def _worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url, new=1)
        except Exception as e:
            log_engine(f"browser open failed: {type(e).__name__}: {e}")

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


def _load_config() -> Config:
    return ConfigLoader(_config_path()).load_config()


def _rest_client_if_ready() -> Any | None:
    holder = get_credentials_holder()
    if not holder.credentials:
        return None
    try:
        from system.ig_rest_session import ensure_shared_authenticated

        return ensure_shared_authenticated(holder.credentials)
    except Exception as e:
        log_engine(f"IG REST session skipped: {type(e).__name__}: {e}")
        return None


class AgentRuntime:
    """Process runtime — trading loop + API server."""

    def __init__(self) -> None:
        self.trading_loop: Any | None = None
        self._stream_client: Any | None = None
        self._shutting_down = False

    def shutdown(self, *, source: str = "runtime") -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log_engine(f"shutdown: graceful teardown (source={source})")
        self._stream_client = None
        from system.shutdown_cleanup import perform_shutdown_cleanup

        perform_shutdown_cleanup(source=source)
        if source not in ("dashboard", "api"):
            try:
                from system.telegram_notifier import send_critical_alert

                send_critical_alert(f"🛑 Agent stopped (source: {source})")
            except Exception as e:
                log_engine(f"telegram shutdown notify failed: {type(e).__name__}: {e}")
        log_engine("shutdown complete")

    def run(self) -> int:
        code = run_preflight()
        if code != EXIT_OK:
            if code == EXIT_INSTANCE:
                release_instance_lock()
            return code

        if not check_port_available(_API_PORT):
            print(_port_in_use_banner(_API_PORT), file=sys.stderr)
            release_instance_lock()
            sys.exit(1)

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        os.environ.setdefault("PYTHONPATH", str(project_root() / "src"))
        logs_dir().mkdir(parents=True, exist_ok=True)

        try:
            cfg = _load_config()
            merged = apply_config_defaults(cfg.as_dict())
            cfg = Config(_data=merged)
            n_instruments = len(
                [
                    k
                    for k, v in (cfg.get("instruments") or {}).items()
                    if isinstance(v, dict) and v.get("enabled")
                ]
            )
            _startup_mark(
                "config", note=f"{n_instruments} instruments" if n_instruments else None
            )

            rest = _rest_client_if_ready()
            from api.snapshot_store import wire_hub_quotes_to_dashboard
            from runtime.agent_bootstrap import (
                build_market_orchestrator,
                start_market_stream,
            )
            from runtime.ig_account_verify import verify_account_on_broker
            from system.credentials_loader import try_load_credentials

            cred_status = try_load_credentials()
            if rest is not None and cred_status.ok and cred_status.credentials:
                verify_account_on_broker(rest, cred_status.credentials)
            _startup_mark(
                "ig_auth",
                note="demo account" if rest is not None else "credentials not loaded",
            )

            self.trading_loop = build_market_orchestrator(cfg, rest_client=rest)
            register_trading_loop(self.trading_loop)

            def _start_live_engines() -> None:
                from api.agent_control import start_trading

                wire_hub_quotes_to_dashboard(min_interval=0.25)
                self._stream_client = start_market_stream(cfg, rest_client=rest)
                _startup_mark("stream")
                # Auto-start trading loops — no dashboard Start button required.
                result = start_trading()
                if not result.get("ok"):
                    self.trading_loop.start()
                _startup_mark("ready")
                from api.agent_health import start_health_cache_refresher
                from system.replay_daily_scheduler import start_replay_daily_scheduler
                from system.trading_health_monitor import start_trading_health_monitor

                start_replay_daily_scheduler()
                start_health_cache_refresher()
                start_trading_health_monitor()
                try:
                    from data.learning_store import LearningStore
                    from system.paths import data_dir
                    from system.setup_registry_refresh import (
                        refresh_setup_registry_from_store,
                    )

                    store = LearningStore(data_dir() / "learning_db.sqlite3")
                    summary = refresh_setup_registry_from_store(store, enabled=True)
                    log_engine(
                        "setup_registry refreshed at startup: "
                        f"banned={summary.get('banned_count')} "
                        f"gate={'on' if summary.get('enabled') else 'off'}"
                    )
                except Exception as e:
                    log_engine(
                        f"setup_registry startup refresh skipped: "
                        f"{type(e).__name__}: {e}"
                    )
                from system.gate_coherence_scheduler import (
                    start_gate_coherence_scheduler,
                )
                from system.telegram_alerts import (
                    start_hourly_executive_telegram_scheduler,
                )
                from system.v26_shadow_service import start_v26_shadow_service

                start_v26_shadow_service()
                start_gate_coherence_scheduler()
                start_hourly_executive_telegram_scheduler()
                try:
                    from ai.operational.system_monitor import get_system_monitor

                    get_system_monitor().run_background()
                    log_engine("v27 sentinel monitor started (background)")
                except Exception as e:
                    log_engine(f"v27 sentinel monitor failed: {type(e).__name__}: {e}")
                _start_session_refresh_watchdog(rest)
                log_engine("orchestrator trading loop started (background)")
                try:
                    from system.telegram_notifier import send_critical_alert

                    send_critical_alert("✅ Agent started — trading loops active")
                except Exception as e:
                    log_engine(
                        f"telegram startup alert failed: {type(e).__name__}: {e}"
                    )
                from system.engine_log import _intermittent_settings

                on, iv = _intermittent_settings()
                if on:
                    log_engine(
                        f"Intermittent engine logging enabled "
                        f"(stream/hub quotes every {iv:.0f}s per epic)"
                    )

            register_api_startup(_ensure_watchdog_running)
            register_api_startup(_start_live_engines)

            app = create_app(watch_snapshot=True)
            if not os.environ.get("IG_AGENT_FROM_LAUNCHER"):
                _open_browser_delayed(_DASHBOARD_URL)

            import uvicorn

            log_engine(f"API server: binding on port {_API_PORT}")
            uvicorn.run(app, host=_API_HOST, port=_API_PORT, log_level="info")
            return EXIT_OK
        finally:
            self.shutdown(source="normal")


def _install_signal_handlers(runtime: AgentRuntime) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        log_engine(f"signal {signum} received — graceful shutdown")
        runtime.shutdown(source=f"signal:{signum}")
        raise SystemExit(128 + (signum if signum < 128 else 0))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


def main() -> None:
    atexit.register(_force_cleanup_port)
    _rotate_oversized_logs()
    log_engine(f"=== {APP_DISPLAY_NAME} {APP_VERSION_LABEL} full restart ===")
    _pre_startup_cleanup()
    runtime = AgentRuntime()
    _install_signal_handlers(runtime)
    try:
        raise SystemExit(runtime.run())
    except SystemExit as exc:
        if exc.code not in (None, 0):
            runtime.shutdown()
        raise
    except Exception as e:
        log_engine(f"CRITICAL: {type(e).__name__}: {e}")
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(f"Agent crash — {type(e).__name__}: {e}")
        except Exception:
            pass
        runtime.shutdown(source="crash")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
