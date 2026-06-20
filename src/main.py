"""
IG Agent v29 entry point — launchd / manual start.

Preflight: emergency lock, config validation, demo guard, instance lock, credentials.
Runtime: trading loop (background) + FastAPI on profile API port (foreground).
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
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_LOCK = 2
EXIT_CONFIG = 3
EXIT_INSTANCE = 4

_BROWSER_DELAY_SEC = 3.0
_LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — rotate shell-written logs
_LOG_KEEP_BACKUPS = 3
_API_HOST = "127.0.0.1"
_DESKTOP_API_PORT = 9090


def _align_desktop_api_port() -> None:
    """Apex Electron shell — force shadow :9090 regardless of NODE_ENV leakage."""
    if os.environ.get("IG_APEX_DESKTOP", "").strip() != "1" and (
        os.environ.get("IG_AGENT_DESKTOP_LAUNCH", "").strip() != "1"
    ):
        return
    os.environ["IG_API_PORT"] = str(_DESKTOP_API_PORT)
    os.environ["IG_NODE_PROFILE"] = "shadow"
    os.environ["NODE_ENV"] = "shadow"


def _api_port() -> int:
    _align_desktop_api_port()
    try:
        from system.node_profile import get_node_profile

        return int(get_node_profile().api_port)
    except Exception:
        raw = os.environ.get("IG_API_PORT", "").strip()
        if raw.isdigit():
            return int(raw)
        if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
            return _DESKTOP_API_PORT
        return int(os.environ.get("IG_API_PORT", "8080"))


def _dashboard_url() -> str:
    try:
        from system.node_profile import get_node_profile

        return get_node_profile().dashboard_url
    except Exception:
        if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
            return f"http://127.0.0.1:{_DESKTOP_API_PORT}/"
        return "http://localhost:8080/"


def _log_engine(message: str) -> None:
    from system.engine_log import log_engine

    log_engine(message)


def _startup_mark(phase_id: str, note: str | None = None) -> None:
    try:
        from system.startup_tracker import mark

        mark(phase_id, note)
    except Exception:
        pass


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
        _log_engine(f"telegram early init failed: {type(e).__name__}: {e}")


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
    """Return True when 127.0.0.1:port is free to bind (no localhost DNS)."""
    import socket

    host = "127.0.0.1"
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _port_in_use_banner(port: int) -> str:
    from system.app_identity import APP_DISPLAY_NAME

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
    from system.config_loader import ConfigLoader

    return ConfigLoader(_config_path()).load_config(validate=False).as_dict()


def merge_credentials_for_validation(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay IG credentials from credentials.json for validator critical keys."""
    from system.credentials_loader import try_load_credentials

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
    """Remove all .pyc and __pycache__ under project root — fresh bytecode on launch."""
    _purge_workspace_bytecode()


def _purge_workspace_bytecode() -> None:
    """Absolute workspace bytecode purge (mirrors find . -name '*.pyc' / __pycache__)."""
    import shutil

    from system.paths import project_root

    root = project_root()
    pyc_removed = 0
    cache_removed = 0
    for pyc in root.rglob("*.pyc"):
        try:
            pyc.unlink()
            pyc_removed += 1
        except Exception:
            pass
    for cache_dir in list(root.rglob("__pycache__")):
        try:
            shutil.rmtree(cache_dir)
            cache_removed += 1
        except Exception:
            pass
    _log_engine(
        f"startup: purged workspace bytecode "
        f"({pyc_removed} .pyc, {cache_removed} __pycache__ dirs)"
    )


def _exchange_rollover_emergence_pause() -> None:
    """
    Exchange rollover emergence pause — 22:58–23:02 Europe/London (BST/GMT).

    Blocks boot briefly so IG daily accounting log wrap completes before trading init.
    """
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        from datetime import datetime

        now = datetime.now()
    in_window = (now.hour == 22 and now.minute >= 58) or (
        now.hour == 23 and now.minute <= 2
    )
    if not in_window:
        return
    _log_engine(
        "rollover pause: 22:58–23:02 Europe/London — "
        f"sleeping 240s (now {now.strftime('%H:%M:%S %Z')})"
    )
    time.sleep(240)
    _log_engine("rollover pause: complete — resuming boot")


def _resolve_killable_pid(raw_pid: str | int) -> int | None:
    """
    Normalize pgrep/lsof PID text and skip protected processes.

    Returns an int PID to signal, or None when invalid / self / parent.
    """
    clean_target_str = str(raw_pid).strip()
    if not clean_target_str.isdigit():
        return None

    target_pid_int = int(clean_target_str)

    if target_pid_int == os.getpid() or target_pid_int == os.getppid():
        return None

    return target_pid_int


def _pre_startup_kill_orphan_agents(*, wait_sec: float = 1.0) -> list[int]:
    """SIGTERM (then optional SIGKILL) stale src/main.py processes — never self."""
    if os.environ.get("IG_AGENT_PYTEST") == "1":
        return []
    try:
        from system.node_profile import is_shadow_node

        if is_shadow_node():
            _log_engine(
                "pre-startup: orphan kill skipped (shadow profile — production :8080 protected)"
            )
            return []
    except Exception:
        pass
    if os.environ.get("IG_AGENT_SKIP_ORPHAN_KILL", "").strip() in ("1", "true", "yes"):
        _log_engine("pre-startup: orphan kill skipped (IG_AGENT_SKIP_ORPHAN_KILL=1)")
        return []
    killed: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            target_pid_int = _resolve_killable_pid(pid_str)
            if target_pid_int is None:
                continue
            try:
                os.kill(target_pid_int, signal.SIGTERM)
                killed.append(target_pid_int)
                _log_engine(
                    f"pre-startup: SIGTERM orphan agent PID {target_pid_int}"
                )
            except ProcessLookupError:
                pass
            except Exception as e:
                _log_engine(
                    f"pre-startup: could not SIGTERM PID {target_pid_int}: {e}"
                )
    except Exception as e:
        _log_engine(f"pre-startup: pgrep failed: {e}")
        return killed

    if not killed:
        return killed

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        alive: list[int] = []
        for target_pid in killed:
            if _resolve_killable_pid(target_pid) is None:
                continue
            try:
                os.kill(target_pid, 0)
                alive.append(target_pid)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if not alive:
            break
        time.sleep(0.2)

    for target_pid in killed:
        if _resolve_killable_pid(target_pid) is None:
            continue
        try:
            os.kill(target_pid, 0)
            os.kill(target_pid, signal.SIGKILL)
            _log_engine(f"pre-startup: SIGKILL orphan agent PID {target_pid}")
        except ProcessLookupError:
            pass
        except Exception:
            pass
    return killed


def _pre_startup_cleanup() -> None:
    """Kill stale processes and release resources before Gate 1 acquires the instance lock."""
    from system.instance_lock import lock_path as instance_lock_path

    if os.environ.get("IG_AGENT_CLEAR_PYCACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        _clear_pycache()
    from system.shutdown_cleanup import clear_manual_stop

    clear_manual_stop()
    _init_telegram_from_config()

    lock_file = instance_lock_path()

    # 1. Find and SIGTERM any other agent processes (never this PID).
    killed_pids = _pre_startup_kill_orphan_agents(wait_sec=1.0)

    # 2b. Orphan watchdog from a prior session can race this startup — stop it first.
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if not launchd_watchdog_active():
            from api.agent_health import stop_watchdog

            stop_watchdog(preserve_launchd=False)
            _log_engine("pre-startup: cleared standalone watchdog from prior session")
    except Exception as e:
        _log_engine(f"pre-startup: watchdog cleanup error (ignored): {e}")

    # 3. Remove stale lock
    try:
        if lock_file.exists():
            lock_file.unlink()
            _log_engine("pre-startup: removed stale instance lock")
    except Exception as e:
        _log_engine(f"pre-startup: could not remove lock: {e}")

    # 4. Kill any process still bound to API port (skip production ports in shadow desktop)
    protect_prod = os.environ.get("IG_APEX_PROTECT_PRODUCTION_PORTS", "").strip() in (
        "1",
        "true",
        "yes",
    )
    port = _api_port()
    if not (protect_prod and port in (8080, 8787)):
        _force_cleanup_port(port)

    # 5. Wait for API port to be free (previous server may still be tearing down)
    import socket as _socket

    _port_free = False
    for _ in range(10):  # up to 3 s
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                _port_free = True
                break
            except OSError:
                pass
        time.sleep(0.3)
    if not _port_free:
        _log_engine(
            f"pre-startup: port {port} still in use after cleanup — proceeding anyway"
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
            _log_engine(
                f"pre-startup: cleared {cleared_pending} stale pending order(s) "
                f"and {cleared_inflight} in-flight entry/ies from previous session"
            )
    except Exception as e:
        _log_engine(f"pre-startup: inflight/pending clear failed (ignored): {e}")

    # 7. Mark startup phase (visible in splash screen)
    note = (
        f"killed {len(killed_pids)} previous session(s)"
        if killed_pids
        else "no previous session running"
    )
    _startup_mark("session_cleanup", note)
    _log_engine(f"pre-startup: cleanup complete — {note}")


def _ensure_watchdog_running() -> None:
    """Start scripts/watchdog.sh when absent — skip if launchd already owns supervision."""
    from system.paths import logs_dir, project_root

    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            _log_engine(
                "startup: launchd watchdog active — skipping manual watchdog spawn"
            )
            return
    except Exception:
        pass

    try:
        from api.agent_health import _watchdog_active

        if _watchdog_active():
            _log_engine("startup: watchdog already running")
            return
    except Exception:
        pass

    wd = project_root() / "scripts" / "watchdog.sh"
    if not wd.is_file():
        _log_engine(f"startup: watchdog script missing ({wd})")
        return
    if not os.access(wd, os.X_OK):
        _log_engine("startup: watchdog script not executable")
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
        _log_engine("startup: watchdog started")
    except Exception as e:
        _log_engine(f"startup: watchdog start failed: {type(e).__name__}: {e}")


def _force_cleanup_port(port: int | None = None) -> None:
    """Delegate to shared port eviction (startup, atexit, shutdown_cleanup)."""
    from system.boot.port_eviction import reclaim_api_port

    reclaim_api_port(port if port is not None else _api_port())


def run_preflight() -> int:
    """Steps 1–4. Returns exit code (0 = continue)."""
    try:
        from system.env_loader import prepare_boot_env
        from system.node_profile import apply_node_profile_to_environ

        prepare_boot_env()
        apply_node_profile_to_environ()
    except Exception as e:
        _log_engine(f"boot env prepare skipped: {type(e).__name__}: {e}")

    api_port = _api_port()
    from system.boot.port_eviction import reclaim_and_wait

    if not reclaim_and_wait(api_port):
        from system.app_identity import APP_DISPLAY_NAME

        print(
            f"{APP_DISPLAY_NAME}: port {api_port} is already in use — stop the other process first.",
            file=sys.stderr,
        )
        return EXIT_INSTANCE

    from system.app_identity import APP_DISPLAY_NAME
    from system.config_validator import emergency_stop_lock_present, validate_config
    from system.credentials_holder import bootstrap_credentials
    from system.instance_lock import acquire_instance_lock

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
        _log_engine(f"preflight: {demo_msg}")
    except Exception as e:
        _log_engine(f"preflight: demo guard error (continuing): {type(e).__name__}: {e}")

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
        _log_engine(f"credentials bootstrap: loaded ({holder.credentials.account_type})")
    else:
        _log_engine(f"credentials bootstrap: not ready — {holder.status.error}")

    _startup_mark("preflight")
    return EXIT_OK


def _open_browser_delayed(url: str, delay: float = _BROWSER_DELAY_SEC) -> None:
    """Permanently disabled — backend must never spawn an external browser."""
    _log_engine(f"browser auto-launch disabled (use Electron or open manually): {url}")


class AgentRuntime:
    """Process runtime — trading loop + API server."""

    def __init__(self, *, boot_context: Any | None = None) -> None:
        self.trading_loop: Any | None = None
        self._stream_client: Any | None = None
        self._shutting_down = False
        self._boot_context = boot_context
        self._uvicorn_server: Any | None = None

    def shutdown(self, *, source: str = "runtime") -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        _log_engine(f"shutdown: graceful teardown (source={source})")
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        self._stream_client = None
        from system.shutdown_cleanup import perform_shutdown_cleanup

        perform_shutdown_cleanup(source=source)
        if source not in ("dashboard", "api"):
            try:
                from system.telegram_notifier import send_critical_alert

                send_critical_alert(f"🛑 Agent stopped (source: {source})")
            except Exception as e:
                _log_engine(f"telegram shutdown notify failed: {type(e).__name__}: {e}")
        _log_engine("shutdown complete")

    def run(self) -> int:
        from api.server import create_app
        from system.instance_lock import release_instance_lock
        from system.paths import logs_dir, project_root
        from system.system_state import get_system_state

        if not get_system_state().gate_complete("G1"):
            code = run_preflight()
            if code != EXIT_OK:
                if code == EXIT_INSTANCE:
                    release_instance_lock()
                return code

        from system.boot.port_eviction import reclaim_and_wait

        bind_port = _api_port()
        if not reclaim_and_wait(bind_port):
            print(_port_in_use_banner(bind_port), file=sys.stderr)
            release_instance_lock()
            sys.exit(1)

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        os.environ.setdefault("PYTHONPATH", str(project_root() / "src"))
        logs_dir().mkdir(parents=True, exist_ok=True)

        try:
            _ensure_watchdog_running()

            app = create_app(
                watch_snapshot=True,
                use_boot_pipeline=True,
                boot_context=self._boot_context,
            )
            if not os.environ.get("IG_AGENT_FROM_LAUNCHER"):
                _open_browser_delayed(_dashboard_url())

            import uvicorn

            bind_port = _api_port()
            _log_engine(f"API server: binding on port {bind_port}")
            config = uvicorn.Config(
                app, host=_API_HOST, port=bind_port, log_level="info"
            )
            server = uvicorn.Server(config)
            self._uvicorn_server = server
            server.run()
            return EXIT_OK
        finally:
            self.shutdown(source="normal")


def _install_signal_handlers(runtime: AgentRuntime) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        _log_engine(f"signal {signum} received — graceful shutdown")
        runtime.shutdown(source=f"signal:{signum}")
        raise SystemExit(128 + (signum if signum < 128 else 0))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass



def main() -> None:
    from system.env_loader import load_dotenv, prepare_boot_env
    from system.node_profile import apply_node_profile_to_environ, get_node_profile
    from system.paths import project_root
    from system.system_state import stamp_process_boot_start

    os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
    _align_desktop_api_port()
    # Finder/GUI launches inherit stale shell IG_* keys — .env must win.
    _from_launcher = os.environ.get("IG_AGENT_FROM_LAUNCHER") == "1" or (
        os.environ.get("IG_AGENT_DESKTOP_LAUNCH") == "1"
    )
    load_dotenv(override=_from_launcher)
    prepare_boot_env()
    from system.apex_runtime_mode import apply_runtime_mode_to_environ

    apply_runtime_mode_to_environ()
    profile = apply_node_profile_to_environ()
    stamp_process_boot_start()

    from system.app_identity import APP_DISPLAY_NAME, APP_VERSION_LABEL
    from system.boot.exceptions import Gate1FatalError
    from system.boot.gate1_preflight import run_gate1_preflight
    from system.system_state import BootPhase, get_system_state

    _desktop_fast_bind = os.environ.get("IG_APEX_DESKTOP", "").strip() == "1"

    atexit.register(lambda: _force_cleanup_port(_api_port()))
    _rotate_oversized_logs()
    _log_engine(
        f"=== {APP_DISPLAY_NAME} {APP_VERSION_LABEL} full restart "
        f"(node={profile.kind} api=:{profile.api_port}) ==="
    )
    _exchange_rollover_emergence_pause()
    _purge_workspace_bytecode()
    if _desktop_fast_bind:
        try:
            from apex.ipc_bridge import start_ipc_bridge_daemon
            from apex.microkernel import start_microkernel
            from execution.atomic_gateway import set_monitoring_mode

            # Boot gates need passive IG REST (accounts, positions, OHLC).
            set_monitoring_mode(False)
            start_ipc_bridge_daemon()
            start_microkernel(workers_only=True)
            _log_engine("Apex desktop: IPC bridge + micro-kernel armed (workers only)")
        except Exception as exc:
            _log_engine(f"Apex desktop bootstrap failed: {type(exc).__name__}: {exc}")
        threading.Thread(
            target=_pre_startup_cleanup,
            name="pre-startup-cleanup",
            daemon=True,
        ).start()
    else:
        _pre_startup_cleanup()

    boot_ctx = None
    if _desktop_fast_bind:
        _log_engine(
            "Apex desktop: deferring Gate1 to post-bind lifespan (fast Electron handshake)"
        )
    else:
        try:
            boot_ctx = run_gate1_preflight()
        except Gate1FatalError as exc:
            sys.exit(exc.exit_code)

    runtime = AgentRuntime(boot_context=boot_ctx)
    _install_signal_handlers(runtime)
    try:
        raise SystemExit(runtime.run())
    except SystemExit as exc:
        if exc.code not in (None, 0):
            runtime.shutdown()
        raise
    except Exception as e:
        _log_engine(f"CRITICAL: {type(e).__name__}: {e}")
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(f"Agent crash — {type(e).__name__}: {e}")
        except Exception:
            pass
        runtime.shutdown(source="crash")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
