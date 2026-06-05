"""
IG Agent v25 entry point — launchd / manual start (Section 4.5 Step 12).

Preflight: emergency lock, config validation, instance lock, credentials.
Runtime: trading loop (background) + FastAPI on :8080 (foreground).
"""

from __future__ import annotations

import atexit
import json
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
from system.instance_lock import acquire_instance_lock, release_instance_lock
from system.paths import config_dir, logs_dir, project_root

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
        f"IG Agent v25: port {port} is already in use\n"
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
    return config_dir() / "config_v25.json"


def load_raw_config_dict() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    return data


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


def _pre_startup_cleanup() -> None:
    """Kill any stale agent processes and release resources before acquiring a new lock.

    Runs every time the agent starts so a previous crash, force-quit, or silent
    background session never blocks the next launch.
    """
    _clear_pycache()

    import os

    my_pid = os.getpid()
    lock_path = Path(__file__).parent / "data" / ".ig_agent_v25.lock"
    killed_pids: list[int] = []

    # 1. Find and SIGTERM any other agent processes
    try:
        result = subprocess.run(
            ["pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            if pid == my_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed_pids.append(pid)
                log_engine(f"pre-startup: sent SIGTERM to previous session PID {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                log_engine(f"pre-startup: could not kill PID {pid}: {e}")
    except Exception as e:
        log_engine(f"pre-startup: pgrep failed: {e}")

    # 2. Wait up to 5 s for terminated processes to release port 8080 and lock
    if killed_pids:
        _deadline = time.time() + 5.0
        while time.time() < _deadline:
            still_alive = []
            for pid in killed_pids:
                try:
                    os.kill(pid, 0)  # 0 = probe only, raises if dead
                    still_alive.append(pid)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if not still_alive:
                break
            time.sleep(0.3)
        # SIGKILL any survivors
        for pid in killed_pids:
            try:
                os.kill(pid, signal.SIGKILL)
                log_engine(f"pre-startup: SIGKILL fallback for PID {pid}")
            except ProcessLookupError:
                pass
            except Exception:
                pass

    # 3. Remove stale lock
    try:
        if lock_path.exists():
            lock_path.unlink()
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


def _force_cleanup_port(port: int = 8080) -> None:
    """Kill any process listening on *port* (other than self) and remove the lock.

    Uses ``lsof -ti :<port>`` which catches zombie uvicorn workers that
    ``pgrep -f src/main.py`` misses.  Safe to call at startup and on exit.
    """
    own_pid = os.getpid()
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
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
    lock = Path(__file__).parent / "data" / ".ig_agent_v25.lock"
    lock.unlink(missing_ok=True)


def run_preflight() -> int:
    """Steps 1–4. Returns exit code (0 = continue)."""
    if emergency_stop_lock_present():
        print(
            "IG Agent v25: emergency_stop.lock present — delete it to restart.",
            file=sys.stderr,
        )
        return EXIT_LOCK

    try:
        raw = load_raw_config_dict()
    except Exception as e:
        print(f"IG Agent v25: config load failed: {e}", file=sys.stderr)
        return EXIT_CONFIG

    validation_cfg = merge_credentials_for_validation(raw)
    valid, messages = validate_config(validation_cfg)
    if not valid:
        for line in messages:
            if line.startswith("ERROR:"):
                print(f"IG Agent v25: {line}", file=sys.stderr)
        return EXIT_CONFIG

    ok, msg = acquire_instance_lock()
    if not ok:
        try:
            if not _is_benign_startup_lock_failure(msg):
                from system.watchdog_banner import record_startup_failure

                record_startup_failure(msg)
        except Exception:
            pass
        print(f"IG Agent v25: {msg}", file=sys.stderr)
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

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log_engine("shutdown: stopping trading loop")
        if self.trading_loop is not None:
            try:
                self.trading_loop.stop()
            except Exception as e:
                log_engine(f"trading loop stop failed: {type(e).__name__}: {e}")
        if self._stream_client is not None:
            from runtime.agent_bootstrap import stop_market_stream

            stop_market_stream(self._stream_client)
            self._stream_client = None
        try:
            from system.telegram_notifier import (
                get_telegram_notifier,
                stop_telegram_heartbeat,
            )

            notifier = get_telegram_notifier()
            if notifier is not None and notifier.enabled:
                notifier.notify_shutdown()
            stop_telegram_heartbeat()
        except Exception:
            pass
        release_instance_lock()
        _force_cleanup_port()
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
                wire_hub_quotes_to_dashboard(min_interval=0.25)
                self._stream_client = start_market_stream(cfg, rest_client=rest)
                _startup_mark("stream")
                self.trading_loop.start()
                _startup_mark("ready")
                from system.replay_daily_scheduler import start_replay_daily_scheduler

                start_replay_daily_scheduler()
                _start_session_refresh_watchdog(rest)
                log_engine("orchestrator trading loop started (background)")
                from system.engine_log import _intermittent_settings

                on, iv = _intermittent_settings()
                if on:
                    log_engine(
                        f"Intermittent engine logging enabled "
                        f"(stream/hub quotes every {iv:.0f}s per epic)"
                    )

            register_api_startup(_start_live_engines)

            app = create_app(watch_snapshot=True)
            if not os.environ.get("IG_AGENT_FROM_LAUNCHER"):
                _open_browser_delayed(_DASHBOARD_URL)

            import uvicorn

            log_engine(f"API server: started on port {_API_PORT}")
            uvicorn.run(app, host=_API_HOST, port=_API_PORT, log_level="info")
            return EXIT_OK
        finally:
            self.shutdown()


def _install_signal_handlers(runtime: AgentRuntime) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        log_engine(f"signal {signum} received — graceful shutdown")
        runtime.shutdown()
        raise SystemExit(128 + (signum if signum < 128 else 0))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


def main() -> None:
    atexit.register(_force_cleanup_port)
    _rotate_oversized_logs()
    log_engine("=== IG Agent v25 full restart ===")
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
            from system.telegram_notifier import get_telegram_notifier

            notifier = get_telegram_notifier()
            if notifier is not None and notifier.enabled:
                notifier.notify_crash(f"{type(e).__name__}: {e}")
        except Exception:
            pass
        runtime.shutdown()
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
