"""
IG Trading Desk v31.1 entry point — launchd / Trading Desk / manual start.

Preflight: emergency lock, config validation, demo guard, instance lock, credentials.
Runtime: trading loop (background) + FastAPI on profile API port (foreground).
Canonical desktop: Trading_Desk.app → scripts/trading_desk_silent.sh → Quantum Terminal :3000.
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

# Single-String SHM ctypes registry (`/ig_agent_v30_shm`) — fields: ticks_cached,
# signal_threshold, atr_multiplier, valve_status, last_trade_pnl (see CockpitShmHeader).
from system.ipc.ring_buffer import CockpitShmHeader  # noqa: F401

EXIT_OK = 0
EXIT_LOCK = 2
EXIT_CONFIG = 3
EXIT_INSTANCE = 4

_BROWSER_DELAY_SEC = 3.0
_LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — rotate shell-written logs
_LOG_KEEP_BACKUPS = 3
_API_HOST = "127.0.0.1"
_DESKTOP_API_PORT = 9090

_singleton_socket: socket.socket | None = None
_singleton_socket_bound = False


_SINGLETON_BIND_RETRIES = 5
_SINGLETON_BIND_RETRY_SEC = 2.0


_v32_dual_port_mode_cached: bool | None = None


def _skip_v32_singleton_lock() -> bool:
    """True when v32 dual-port CLI triplet is active — twin engines must not collide."""
    global _v32_dual_port_mode_cached
    if _v32_dual_port_mode_cached is not None:
        return _v32_dual_port_mode_cached
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        _v32_dual_port_mode_cached = True
        return True
    try:
        from system.engine_cli import parse_engine_cli

        dual = parse_engine_cli().dual_port_mode
        _v32_dual_port_mode_cached = dual
        return dual
    except Exception:
        _v32_dual_port_mode_cached = False
        return False


def enforce_absolute_socket_singleton() -> None:
    """
    Fail closed when a live twin holds the singleton port.

    Retries briefly first: during a supervisor restart the outgoing instance may
    hold the port for a few seconds while tearing down. Always logs before
    exiting — a silent exit(0) here previously left supervisors with an
    unexplained "agent process died".
    """
    global _singleton_socket, _singleton_socket_bound
    if _skip_v32_singleton_lock():
        return
    if _singleton_socket_bound:
        return
    last_err: OSError | None = None
    for attempt in range(1, _SINGLETON_BIND_RETRIES + 1):
        try:
            _singleton_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # CRITICAL HARDENING: Allow immediate local address re-binding
            _singleton_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _singleton_socket.bind(("127.0.0.1", 49151))
            _singleton_socket.listen(1)
            _singleton_socket_bound = True
            if attempt > 1:
                _log_engine(
                    f"singleton: port 49151 acquired on attempt {attempt} "
                    "(previous instance released during retry window)"
                )
            return
        except OSError as exc:
            last_err = exc
            try:
                if _singleton_socket is not None:
                    _singleton_socket.close()
            except OSError:
                pass
            _singleton_socket = None
            if attempt < _SINGLETON_BIND_RETRIES:
                time.sleep(_SINGLETON_BIND_RETRY_SEC)

    # Fail closed — a true active twin instance is running.
    msg = (
        f"singleton FAIL-CLOSED: port 49151 still held after "
        f"{_SINGLETON_BIND_RETRIES} attempts ({last_err}) — live twin instance; exiting"
    )
    try:
        _log_engine(msg)
    except Exception:
        pass
    print(msg, file=sys.stderr)
    sys.exit(0)


_IMMUTABLE_BIND_TARGET_MS = 200


def _immutable_boot_choreography_enabled() -> bool:
    from system.boot.non_blocking_bootstrap import non_blocking_boot_enabled

    return non_blocking_boot_enabled()


def _start_pre_bind_watchdog() -> None:
    try:
        from system.boot.pre_bind_watchdog import start_pre_bind_watchdog

        start_pre_bind_watchdog(port=_api_port())
    except Exception as exc:
        _log_engine(f"boot_watchdog: start skipped ({type(exc).__name__})")


def _align_desktop_api_port() -> None:
    """Apex Electron shell — force shadow :9090 regardless of NODE_ENV leakage."""
    if os.environ.get("IG_APEX_DESKTOP", "").strip() != "1" and (
        os.environ.get("IG_AGENT_DESKTOP_LAUNCH", "").strip() != "1"
    ):
        return
    # macOS launcher keeps explicit :8080 — only Apex Electron forces shadow :9090.
    if os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1" or (
        os.environ.get("LAUNCHER_DESKTOP", "").strip() == "1"
    ):
        return
    os.environ["IG_API_PORT"] = str(_DESKTOP_API_PORT)
    os.environ["IG_NODE_PROFILE"] = "shadow"
    os.environ["NODE_ENV"] = "shadow"


_boot_milestone_t0: float = 0.0


def _boot_milestone(step: str) -> None:
    global _boot_milestone_t0
    if _boot_milestone_t0 <= 0:
        _boot_milestone_t0 = time.monotonic()
    ms = int((time.monotonic() - _boot_milestone_t0) * 1000)
    _log_engine(f"boot_milestone: {step} +{ms}ms")


def _apply_launcher_desktop_env_early() -> None:
    if os.environ.get("APP_MODE", "").strip().upper() == "TESTBED":
        return
    if os.environ.get("LAUNCHER_DESKTOP", "").strip() == "1" or (
        os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1"
    ):
        os.environ.setdefault("IG_AGENT_DESKTOP_LAUNCH", "1")
        os.environ.pop("IG_APEX_DESKTOP", None)


def _force_launcher_non_blocking_boot() -> None:
    if os.environ.get("LAUNCHER_DESKTOP", "").strip() == "1" or (
        os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1"
    ) or os.environ.get("IG_AGENT_DESKTOP_LAUNCH", "").strip() == "1":
        os.environ["IG_NON_BLOCKING_BOOT"] = "1"
        os.environ.pop("IG_BLOCKING_BOOT", None)


def _defer_heavy_pre_bind_work() -> bool:
    from system.boot.non_blocking_bootstrap import non_blocking_boot_enabled

    if non_blocking_boot_enabled():
        return True
    return (
        os.environ.get("IG_APEX_DESKTOP", "").strip() == "1"
        or os.environ.get("IG_AGENT_FROM_LAUNCHER", "").strip() == "1"
        or os.environ.get("LAUNCHER_DESKTOP", "").strip() == "1"
    )


def _run_deferred_desktop_boot(*, boot_context: Any | None = None) -> None:
    """Heavy boot chores deferred until after API bind on launcher/desktop paths."""
    _boot_milestone("deferred_desktop_boot_start")
    try:
        from system.boot.boot_orchestrator import init_boot_pipeline

        init_boot_pipeline()
    except Exception as exc:
        _log_engine(f"deferred_boot: init_boot_pipeline skipped: {type(exc).__name__}")
    if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
        try:
            from apex.ipc_bridge import start_ipc_bridge_daemon
            from apex.microkernel import start_microkernel
            from execution.atomic_gateway import set_monitoring_mode

            set_monitoring_mode(False)
            start_ipc_bridge_daemon()
            start_microkernel(workers_only=True)
            _log_engine("deferred_boot: Apex IPC bridge + micro-kernel armed")
        except Exception as exc:
            _log_engine(f"deferred_boot: Apex bootstrap failed: {type(exc).__name__}: {exc}")
    _exchange_rollover_emergence_pause()
    _pre_startup_cleanup()
    _boot_milestone("deferred_desktop_boot_done")


try:
    from system.boot.desktop_post_bind import register_post_bind_runner

    register_post_bind_runner(_run_deferred_desktop_boot)
except Exception:
    pass


def _api_port() -> int:
    _align_desktop_api_port()
    try:
        from system.identity.app_identity import RuntimeIdentity

        return RuntimeIdentity.resolve_api_port()
    except Exception:
        raw = os.environ.get("IG_API_PORT", "").strip()
        if raw.isdigit():
            return int(raw)
        if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
            return _DESKTOP_API_PORT
        return 8080


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


def _uvicorn_config_with_reuseport(
    app: Any,
    *,
    host: str,
    port: int,
    log_level: str = "info",
) -> tuple[Any, socket.socket | None]:
    from system.socket_bind import build_uvicorn_config

    return build_uvicorn_config(
        app,
        host=host,
        port=int(port),
        log_level=log_level,
    )


def _parse_harness_ticks(argv: list[str] | None = None) -> int | None:
    """Return tick count when ``--test-harness-ticks=N`` is present."""
    args = list(argv if argv is not None else sys.argv[1:])
    for i, raw in enumerate(args):
        if raw.startswith("--test-harness-ticks="):
            return int(raw.split("=", 1)[1])
        if raw == "--test-harness-ticks" and i + 1 < len(args):
            return int(args[i + 1])
    return None


def _parse_isolated_track(argv: list[str] | None = None) -> str | None:
    """Return ``live`` or ``shadow`` when ``--isolated-track=TRACK`` is present."""
    args = list(argv if argv is not None else sys.argv[1:])
    for i, raw in enumerate(args):
        if raw.startswith("--isolated-track="):
            track = raw.split("=", 1)[1].strip().lower()
            return track if track in ("live", "shadow") else None
        if raw == "--isolated-track" and i + 1 < len(args):
            track = args[i + 1].strip().lower()
            return track if track in ("live", "shadow") else None
    env_track = os.environ.get("IG_PARALLEL_TRACK", "").strip().lower()
    if env_track in ("live", "shadow"):
        return env_track
    return None


def _parse_daemon_cycle(argv: list[str] | None = None) -> int | None:
    """Return cycle interval seconds when ``--daemon-cycle=N`` is present."""
    args = list(argv if argv is not None else sys.argv[1:])
    for i, raw in enumerate(args):
        if raw.startswith("--daemon-cycle="):
            return int(raw.split("=", 1)[1])
        if raw == "--daemon-cycle" and i + 1 < len(args):
            return int(args[i + 1])
    return None


def _is_test_harness_mode() -> bool:
    return os.environ.get("IG_TEST_HARNESS", "").strip() == "1"


def _is_daemon_cycle_mode() -> bool:
    from system.daemon_cycle_kernel import is_daemon_cycle_mode

    return is_daemon_cycle_mode()


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
    try:
        from system.demo_execution_plane import demo_throughput_active

        if demo_throughput_active():
            _log_engine(
                "rollover pause: skipped — demo_throughput_mode "
                "(order gates use live IG market_status)"
            )
            return
    except Exception:
        pass
    try:
        from system.config_loader import ConfigLoader

        cfg = ConfigLoader().load()
        lc = cfg.get("live_canary") if hasattr(cfg, "get") else {}
        if isinstance(lc, dict) and lc.get("enabled") and lc.get("skip_rollover_pause"):
            _log_engine("rollover pause: skipped — live_canary.skip_rollover_pause")
            return
        ep = cfg.get("entry_protection") if hasattr(cfg, "get") else {}
        po = ep.get("premium_overnight") if isinstance(ep, dict) else {}
        if isinstance(po, dict) and po.get("lockdown_permanent"):
            _log_engine(
                "rollover pause: skipped — night matrix lockdown "
                "(broker EDITS_ONLY gates orders at dispatch)"
            )
            return
    except Exception:
        pass
    # Sleep only until end of window (23:03:00 London), not a fixed 240s.
    try:
        from zoneinfo import ZoneInfo

        end = datetime.now(ZoneInfo("Europe/London")).replace(
            hour=23, minute=3, second=0, microsecond=0
        )
        wait_sec = max(1, int((end - now).total_seconds()))
    except Exception:
        wait_sec = 240
    _log_engine(
        "rollover pause: 22:58–23:02 Europe/London — "
        f"sleeping {wait_sec}s (now {now.strftime('%H:%M:%S %Z')})"
    )
    time.sleep(wait_sec)
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

    try:
        from simulation.testbed_daemon import is_protected_pid, zombie_protection_enabled

        if zombie_protection_enabled() and is_protected_pid(target_pid_int):
            return None
    except Exception:
        pass

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
    if _skip_v32_singleton_lock():
        _log_engine(
            "pre-startup: orphan kill skipped (v32 dual-port — sibling engine protected)"
        )
        return []
    try:
        from simulation.testbed_daemon import zombie_protection_enabled

        if zombie_protection_enabled():
            _log_engine(
                "pre-startup: orphan kill skipped (TESTBED_ALLOW_ZOMBIE=1 — "
                "daemon PID protected)"
            )
            return []
    except Exception:
        pass
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
    """Synchronous pre-flight — idempotent lock acquire; fail-closed on live sibling."""
    from system.identity.app_identity import RuntimeIdentity
    from system.identity.instance_lock import acquire_instance_lock, pid_alive, read_lock_holder

    RuntimeIdentity.export_pointer_for_scripts()
    port = RuntimeIdentity.resolve_api_port()
    os.environ["IG_API_PORT"] = str(port)
    lock_target = RuntimeIdentity.get_lock_path()

    if (
        not _is_test_harness_mode()
        and not _is_daemon_cycle_mode()
        and not _skip_v32_singleton_lock()
    ):
        _pre_startup_kill_orphan_agents()

    if os.environ.get("IG_AGENT_CLEAR_PYCACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ) and not _skip_v32_singleton_lock():
        _clear_pycache()

    if os.environ.get("TESTBED_ALLOW_ZOMBIE", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ) and not _is_test_harness_mode():
        parallel_track = os.environ.get("IG_PARALLEL_TRACK", "").strip()
        orchestrator_child = os.environ.get("IG_ORCHESTRATOR_CHILD", "").strip() == "1"
        if parallel_track not in ("live", "shadow") and not orchestrator_child:
            from system.shutdown_cleanup import clear_manual_stop

            clear_manual_stop()
        if parallel_track not in ("live", "shadow") and not orchestrator_child:
            try:
                from system.startup_hold_clear import clear_stale_entry_holds_if_flat

                port = int(os.environ.get("IG_API_PORT", os.environ.get("PORT", "8080")))
                result = clear_stale_entry_holds_if_flat(port=port, reason="main_pre_startup")
                if result.get("cleared") or result.get("deploy_hold_cleared"):
                    _log_engine(f"pre-startup: stale entry holds cleared {result}")
            except Exception as exc:
                _log_engine(
                    f"pre-startup: startup_hold_clear skipped: {type(exc).__name__}: {exc}"
                )
    _init_telegram_from_config()

    if not _is_test_harness_mode():
        try:
            from system.bootstrap_sanitizer import run_supervision_self_sanitize

            run_supervision_self_sanitize(repair=True)
        except Exception as exc:
            _log_engine(f"pre-startup: supervision sanitize skipped: {type(exc).__name__}: {exc}")

    my_pid = os.getpid()
    if not _skip_v32_singleton_lock():
        holder = read_lock_holder(lock_target)
        if holder is not None and holder != my_pid and pid_alive(holder):
            _log_engine(
                f"pre-startup FAIL-CLOSED: live sibling pid={holder} holds {lock_target.name}"
            )
            sys.exit(15)

        ok, msg = acquire_instance_lock()
        if not ok:
            _log_engine(f"pre-startup FAIL-CLOSED: {msg}")
            sys.exit(15)

    try:
        from system.overnight_supervision import launchd_watchdog_active

        if not launchd_watchdog_active():
            from api.agent_health import stop_watchdog

            stop_watchdog(preserve_launchd=False)
            _log_engine("pre-startup: cleared standalone watchdog from prior session")
    except Exception as exc:
        _log_engine(f"pre-startup: watchdog cleanup error: {type(exc).__name__}: {exc}")

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
    except Exception as exc:
        _log_engine(
            f"pre-startup: inflight/pending clear failed: {type(exc).__name__}: {exc}"
        )

    _startup_mark("session_cleanup", f"lock={lock_target.name} port={port}")
    _log_engine(
        f"pre-startup: cleanup complete — lock={lock_target.name} port={port} pid={my_pid}"
    )


def _daemon_supervisor_active() -> bool:
    """True when scripts/daemon_supervisor.sh is alive and owns recovery."""
    candidates: list[Path] = []
    data_root = os.environ.get("IG_DATA_ROOT", "").strip()
    if data_root:
        candidates.append(Path(data_root) / "supervisor.pid")
    try:
        from system.paths import project_root as _proj_root

        candidates.append(_proj_root() / "src" / "data" / "v31-production" / "supervisor.pid")
    except Exception:
        pass
    for pid_file in candidates:
        try:
            raw = pid_file.read_text(encoding="utf-8").strip()
            if raw and raw.isdigit():
                os.kill(int(raw), 0)
                return True
        except (OSError, ValueError):
            continue
    try:
        probe = subprocess.run(
            ["/usr/bin/pgrep", "-f", "daemon_supervisor.sh"],
            capture_output=True,
            timeout=5,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _ensure_watchdog_running() -> None:
    """Start scripts/watchdog.sh when absent — skip if launchd already owns supervision."""
    from system.paths import logs_dir, project_root

    if _skip_v32_singleton_lock():
        _log_engine(
            "startup: v32 dual-port — skipping standalone watchdog.sh "
            "(v32_runtime_start / com.igagent.v32.dual owns supervision)"
        )
        return

    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            _log_engine(
                "startup: launchd watchdog active — skipping manual watchdog spawn"
            )
            return
    except Exception:
        pass

    # Never stack a second supervision layer on top of daemon_supervisor.sh —
    # dueling supervisors have SIGKILLed healthy booting agents (a watchdog
    # spawned here saw an unbound port + missing lock mid-boot and reaped the
    # very process that spawned it).
    try:
        if _daemon_supervisor_active():
            _log_engine(
                "startup: daemon_supervisor active — skipping watchdog.sh spawn "
                "(supervisor owns recovery)"
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


def _arm_boot_sot_fallback_circuit(reason: str = "main_preflight") -> None:
    """Arm stale-cache SoT fallback — hydrate from broker_snapshot on network stall."""
    try:
        from runtime.boot_sot_fallback import arm_boot_fallback_circuit
        from runtime.desk_stability_harness import note_boot_started

        note_boot_started()
        arm_boot_fallback_circuit(reason=reason)
        _log_engine(f"boot_sot_fallback: circuit armed ({reason})")
    except Exception as e:
        _log_engine(f"boot_sot_fallback: arm skipped: {type(e).__name__}: {e}")


def run_preflight() -> int:
    """Steps 1–4. Returns exit code (0 = continue)."""
    if not _skip_v32_singleton_lock():
        enforce_absolute_socket_singleton()
    try:
        from system.env_loader import prepare_boot_env
        from system.node_profile import apply_node_profile_to_environ

        prepare_boot_env()
        apply_node_profile_to_environ()
    except Exception as e:
        _log_engine(f"boot env prepare skipped: {type(e).__name__}: {e}")

    _arm_boot_sot_fallback_circuit("main_preflight")

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


def run_minimal_preflight() -> int:
    """Lock + config validation only — credentials deferred to post-bind background."""
    try:
        from system.env_loader import prepare_boot_env
        from system.node_profile import apply_node_profile_to_environ

        prepare_boot_env()
        apply_node_profile_to_environ()
    except Exception as e:
        _log_engine(f"boot env prepare skipped: {type(e).__name__}: {e}")

    _arm_boot_sot_fallback_circuit("main_minimal_preflight")

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

    _log_engine("preflight: minimal (credentials deferred to post-bind)")
    _startup_mark("preflight")
    return EXIT_OK


def _open_browser_delayed(url: str, delay: float = _BROWSER_DELAY_SEC) -> None:
    """Permanently disabled — backend must never spawn an external browser."""
    _log_engine(f"browser auto-launch disabled (use Electron or open manually): {url}")


def _foreground_execution_guard(shutdown_event: threading.Event) -> None:
    """
    Main-thread foreground keep-alive — holds the process open on macOS Darwin.

    Prevents idle main-thread exit when the monotonic scheduler runs on a worker
    thread. Honors ``shutdown_event`` for SIGTERM/SIGINT graceful teardown.
    """
    _log_engine(
        f"DAEMON-CYCLE: foreground keep-alive armed pid={os.getpid()} — "
        "main thread locked (1s trace)"
    )
    tick = 0
    while not shutdown_event.is_set():
        time.sleep(1)
        tick += 1
        if tick % 30 == 0:
            _log_engine(
                f"DAEMON-CYCLE: live execution heartbeat tick={tick}s pid={os.getpid()}"
            )


class AgentRuntime:
    """Process runtime — trading loop + API server."""

    def __init__(self, *, boot_context: Any | None = None) -> None:
        self.trading_loop: Any | None = None
        self._stream_client: Any | None = None
        self._shutting_down = False
        self._boot_context = boot_context
        self._uvicorn_server: Any | None = None
        self._uvicorn_listen_socket: socket.socket | None = None
        self._boot_degraded = False

    def mark_boot_degraded(self, reason: str) -> None:
        if self._boot_degraded:
            return
        self._boot_degraded = True
        _log_engine(f"boot_degraded: {reason}")
        try:
            from system.system_state import get_system_state

            state = get_system_state()
            if hasattr(state, "note_boot_degraded"):
                state.note_boot_degraded(reason)
        except Exception:
            pass

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

    def run_harness(self, *, tick_count: int) -> int:
        """Boot pipeline + deterministic replay harness; self-terminates on completion."""
        import time

        from system.instance_lock import release_instance_lock
        from system.paths import logs_dir, project_root
        from system.system_state import get_system_state
        from system.test_harness.runner import (
            emit_harness_summary,
            run_harness_tick_phase,
            run_sync_harness_boot,
            wait_for_ready,
        )

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
            return 1

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        os.environ.setdefault("PYTHONPATH", str(project_root() / "src"))
        logs_dir().mkdir(parents=True, exist_ok=True)

        sync_boot = os.environ.get("IG_HARNESS_SYNC_BOOT", "").strip() == "1"
        ctx = self._boot_context

        if sync_boot:
            _log_engine("HARNESS: synchronous turbo boot (no uvicorn)")
            try:
                ctx = run_sync_harness_boot(ctx)
            except Exception as exc:
                _log_engine(
                    f"HARNESS FAIL-CLOSED: sync boot — {type(exc).__name__}: {exc}"
                )
                release_instance_lock()
                return 15
        else:
            import threading

            from api.server import create_app

            app = create_app(
                watch_snapshot=False,
                use_boot_pipeline=True,
                boot_context=self._boot_context,
            )

            import uvicorn

            config, hold_sock = _uvicorn_config_with_reuseport(
                app, host=_API_HOST, port=bind_port, log_level="warning"
            )
            self._uvicorn_listen_socket = hold_sock
            server = uvicorn.Server(config)
            self._uvicorn_server = server

            def _serve() -> None:
                try:
                    server.run()
                except Exception:
                    pass

            thread = threading.Thread(target=_serve, name="harness-uvicorn", daemon=True)
            thread.start()

            _log_engine(f"HARNESS: uvicorn binding :{bind_port} — awaiting READY")
            if not wait_for_ready(timeout_sec=120.0):
                self.shutdown(source="harness_boot_failed")
                release_instance_lock()
                return 15
            ctx = getattr(app.state, "boot_context", None) or self._boot_context

        harness_started = time.monotonic()
        summary = run_harness_tick_phase(tick_count, boot_context=ctx)
        emit_harness_summary(summary)
        _log_engine(
            f"HARNESS: tick phase completed in {time.monotonic() - harness_started:.2f}s"
        )

        exit_code = 0 if summary.ok else 1
        release_instance_lock()
        from system.identity.app_identity import RuntimeIdentity

        lock_path = RuntimeIdentity.get_lock_path()
        if lock_path.is_file():
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as exc:
                _log_engine(f"HARNESS: lock unlink failed: {type(exc).__name__}: {exc}")

        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        os._exit(exit_code)

    def run_daemon_cycle(self, *, interval_sec: float) -> int:
        """Persistent background daemon — API thread + monotonic trading/ML heartbeats."""
        import threading

        from api.server import create_app
        from system.daemon_cycle_kernel import run_monotonic_cycle_loop
        from system.identity.instance_lock import release_instance_lock
        from system.paths import logs_dir, project_root
        from system.system_state import get_system_state
        from system.test_harness.runner import wait_for_ready

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
            return 1

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        os.environ.setdefault("PYTHONPATH", str(project_root() / "src"))
        logs_dir().mkdir(parents=True, exist_ok=True)

        shutdown_event = threading.Event()

        def _handle_daemon_signal(signum: int, _frame: Any) -> None:
            _log_engine(f"DAEMON-CYCLE: signal {signum} — graceful shutdown")
            shutdown_event.set()
            self.shutdown(source=f"signal:{signum}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle_daemon_signal)
            except (ValueError, OSError):
                pass

        try:
            app = create_app(
                watch_snapshot=True,
                use_boot_pipeline=True,
                boot_context=self._boot_context,
            )

            import uvicorn

            _log_engine(
                f"DAEMON-CYCLE: API binding :{bind_port} interval={interval_sec:.0f}s"
            )
            config, hold_sock = _uvicorn_config_with_reuseport(
                app, host=_API_HOST, port=bind_port, log_level="info"
            )
            self._uvicorn_listen_socket = hold_sock
            server = uvicorn.Server(config)
            self._uvicorn_server = server

            def _serve() -> None:
                try:
                    server.run()
                except Exception as exc:
                    _log_engine(
                        f"DAEMON-CYCLE: uvicorn stopped — {type(exc).__name__}: {exc}"
                    )
                finally:
                    shutdown_event.set()

            api_thread = threading.Thread(
                target=_serve, name="daemon-cycle-uvicorn", daemon=True
            )
            api_thread.start()

            if not wait_for_ready(timeout_sec=180.0):
                self.shutdown(source="daemon_boot_failed")
                release_instance_lock()
                return 15

            ctx = getattr(app.state, "boot_context", None) or self._boot_context
            from system.identity.app_identity import RuntimeIdentity

            try:
                from system.agent_execution_mode import authentic_demo_broker_required

                if authentic_demo_broker_required() and ctx is not None:
                    from system.ig_rest_session import force_authenticated_ig_rest_client

                    ctx.rest_client = force_authenticated_ig_rest_client()
            except Exception as exc:
                from system.guard.runtime_guard import log_guarded_exception

                log_guarded_exception("demo_rest_force_daemon", exc)

            preflight_report: dict | None = None
            try:
                from system.production_pipeline_integrity import (
                    verify_production_pipeline_integrity,
                )

                rest_for_preflight = getattr(ctx, "rest_client", None) if ctx else None
                preflight_report = verify_production_pipeline_integrity(
                    rest_client=rest_for_preflight,
                    timeout_sec=10.0,
                    blocking=True,
                )
            except Exception as exc:
                from system.guard.runtime_guard import log_guarded_exception

                log_guarded_exception("verify_production_pipeline_integrity", exc)

            barrier_report: dict | None = None
            if ctx is not None:
                try:
                    from system.bootstrap_phase_barrier import (
                        execute_atomic_bootstrap_phase_barrier,
                    )

                    barrier_report = execute_atomic_bootstrap_phase_barrier(
                        ctx,
                        verify_timeout_sec=5.0,
                        emit=True,
                    )
                    if not barrier_report.get("armed"):
                        _log_engine(
                            "DAEMON-CYCLE: bootstrap phase barrier NOT armed — "
                            f"phase={barrier_report.get('phase')} "
                            f"detail={barrier_report.get('error', '')}"
                        )
                except Exception as exc:
                    from system.guard.runtime_guard import log_guarded_exception

                    log_guarded_exception("bootstrap_phase_barrier", exc)

            lock_path = RuntimeIdentity.get_lock_path()
            _log_engine(
                f"DAEMON-CYCLE: READY pid={os.getpid()} lock={lock_path.name} "
                f"port={bind_port} track={os.environ.get('IG_PARALLEL_TRACK', 'single')} "
                f"— entering monotonic scheduler"
            )

            if os.environ.get("IG_PARALLEL_TRACK", "").strip() == "shadow":
                from system.identity.process_orchestrator import start_shadow_historical_replayer

                start_shadow_historical_replayer(loop=True)
                try:
                    from intelligence.matrix_prebaker import start_alpha_matrix_compiler_async

                    start_alpha_matrix_compiler_async()
                except Exception as exc:
                    from system.guard.runtime_guard import log_guarded_exception

                    log_guarded_exception("alpha_matrix_compiler_boot", exc)

            try:
                from system.ipc.ring_buffer import unified_engine_active
                from system.unified_engine import start_unified_engine

                if unified_engine_active():
                    if barrier_report is not None and barrier_report.get("armed"):
                        start_unified_engine(boot_context=ctx)
                    elif barrier_report is None:
                        _log_engine(
                            "DAEMON-CYCLE: unified engine withheld — no bootstrap context"
                        )
                    else:
                        _log_engine(
                            "DAEMON-CYCLE: unified engine withheld — phase barrier not armed"
                        )
                else:
                    from system.ipc.shm_watchdog import start_shm_watchdog_async

                    start_shm_watchdog_async()
            except Exception as exc:
                from system.guard.runtime_guard import log_guarded_exception

                log_guarded_exception("unified_engine_boot", exc)

            scheduler_thread = threading.Thread(
                target=run_monotonic_cycle_loop,
                kwargs={
                    "interval_sec": interval_sec,
                    "boot_context": ctx,
                    "shutdown_event": shutdown_event,
                },
                name="daemon-monotonic-scheduler",
                daemon=False,
            )
            scheduler_thread.start()
            _foreground_execution_guard(shutdown_event)
            scheduler_thread.join(timeout=30.0)
            return EXIT_OK
        finally:
            self.shutdown(source="daemon_cycle_exit")

    def _run_immutable_fast_bind_server(self) -> int:
        """
        Immutable boot track — bind :8080 first (<200ms target), defer G2/G3 hydration.

        Gate 2/3 execute in background via boot_lifespan (asyncio worker + exponential
        backoff). Main thread blocks only on the API server join.
        """
        from api.server import create_app
        from system.boot.desktop_post_bind import schedule_desktop_post_bind
        from system.boot.non_blocking_bootstrap import (
            schedule_post_bind_maintenance,
            wait_for_api_port,
        )
        from system.instance_lock import release_instance_lock
        from system.paths import logs_dir, project_root
        from system.system_state import get_system_state

        if not get_system_state().gate_complete("G1"):
            code = run_minimal_preflight()
            if code != EXIT_OK:
                if code == EXIT_INSTANCE:
                    release_instance_lock()
                return code

        from system.boot.port_eviction import reclaim_and_wait

        bind_port = _api_port()
        if not reclaim_and_wait(bind_port):
            print(_port_in_use_banner(bind_port), file=sys.stderr)
            release_instance_lock()
            return EXIT_INSTANCE

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        os.environ.setdefault("PYTHONPATH", str(project_root() / "src"))
        logs_dir().mkdir(parents=True, exist_ok=True)

        try:
            app = create_app(
                watch_snapshot=True,
                use_boot_pipeline=True,
                boot_context=self._boot_context,
            )
        except Exception as exc:
            self.mark_boot_degraded(f"immutable_create_app:{type(exc).__name__}")
            release_instance_lock()
            return EXIT_CONFIG
        if not os.environ.get("IG_AGENT_FROM_LAUNCHER"):
            _open_browser_delayed(_dashboard_url())

        import uvicorn

        _start_pre_bind_watchdog()
        bind_started = time.monotonic()
        _log_engine(f"immutable_boot: fast-bind API on :{bind_port}")
        config, hold_sock = _uvicorn_config_with_reuseport(
            app, host=_API_HOST, port=bind_port, log_level="info"
        )
        self._uvicorn_listen_socket = hold_sock
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        def _serve() -> None:
            try:
                server.run()
            except Exception as exc:
                self.mark_boot_degraded(f"immutable_uvicorn:{type(exc).__name__}")
                _log_engine(f"API server stopped — {type(exc).__name__}: {exc}")

        api_thread = threading.Thread(
            target=_serve, name="immutable-boot-uvicorn", daemon=True
        )
        api_thread.start()

        bind_wait = float(os.environ.get("IG_IMMUTABLE_BIND_WAIT_SEC", "45"))
        if wait_for_api_port(_API_HOST, bind_port, timeout_sec=bind_wait):
            bind_ms = int((time.monotonic() - bind_started) * 1000)
            if bind_ms > _IMMUTABLE_BIND_TARGET_MS:
                _log_engine(
                    f"immutable_boot: WARN bind exceeded {_IMMUTABLE_BIND_TARGET_MS}ms "
                    f"(actual={bind_ms}ms)"
                )
            else:
                _log_engine(
                    f"immutable_boot: :{bind_port} bound in {bind_ms}ms "
                    f"(target <{_IMMUTABLE_BIND_TARGET_MS}ms)"
                )
        else:
            _log_engine(
                f"immutable_boot: WARN /api/health not live within {bind_wait:.0f}s "
                f"— continuing (G2/G3 hydration still running in background)"
            )

        schedule_post_bind_maintenance(
            boot_context=self._boot_context,
            purge_bytecode=_defer_heavy_pre_bind_work()
            and not _skip_v32_singleton_lock(),
            install_kernel=_defer_heavy_pre_bind_work(),
        )
        if _defer_heavy_pre_bind_work():
            schedule_desktop_post_bind(boot_context=self._boot_context)
        if not _is_test_harness_mode():
            threading.Thread(
                target=_ensure_watchdog_running,
                name="post-bind-watchdog",
                daemon=True,
            ).start()
        _log_engine(
            f"immutable_boot: /api/health live — G2/G3 hydration deferred to background"
        )
        api_thread.join()
        return EXIT_OK

    def run(self) -> int:
        from api.server import create_app
        from system.instance_lock import release_instance_lock
        from system.paths import logs_dir, project_root
        from system.system_state import get_system_state

        if _immutable_boot_choreography_enabled():
            try:
                return self._run_immutable_fast_bind_server()
            finally:
                self.shutdown(source="normal")

        from system.boot.non_blocking_bootstrap import (
            schedule_post_bind_maintenance,
            wait_for_api_port,
        )

        fast_bind = False
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
            if not _is_test_harness_mode():
                _ensure_watchdog_running()

            try:
                app = create_app(
                    watch_snapshot=True,
                    use_boot_pipeline=True,
                    boot_context=self._boot_context,
                )
            except Exception as exc:
                self.mark_boot_degraded(f"create_app:{type(exc).__name__}")
                raise
            if not os.environ.get("IG_AGENT_FROM_LAUNCHER"):
                _open_browser_delayed(_dashboard_url())

            import uvicorn

            bind_port = _api_port()
            _start_pre_bind_watchdog()
            _log_engine(f"API server: blocking legacy bind on port {bind_port}")
            config, hold_sock = _uvicorn_config_with_reuseport(
                app, host=_API_HOST, port=bind_port, log_level="info"
            )
            self._uvicorn_listen_socket = hold_sock
            server = uvicorn.Server(config)
            self._uvicorn_server = server
            try:
                server.run()
            except Exception as exc:
                self.mark_boot_degraded(f"uvicorn:{type(exc).__name__}")
                raise
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
    global _boot_milestone_t0, _v32_dual_port_mode_cached
    from system.engine_cli import bootstrap_engine_cli, is_v32_dual_port_mode, reapply_engine_cli_env

    _engine_cli = bootstrap_engine_cli()
    _v32_dual_port_mode_cached = _engine_cli.dual_port_mode
    try:
        from system.core_affinity import pin_current_process_to_engine

        pin_current_process_to_engine(_engine_cli.origin)
    except Exception as exc:
        _log_engine(f"core_affinity: bootstrap pin skipped ({type(exc).__name__})")
    os.environ.setdefault("IG_AGENT_IN_PROCESS", "1")
    # ~100 runtime threads contend for the GIL; at the default 5ms switch
    # interval a 100ms Yahoo HTTPS fetch stretched to 2-3s inside this process
    # (measured), tripping every feed timeout and starving the market data hub.
    # 1ms switching restores sub-second socket reads for the feed threads.
    sys.setswitchinterval(0.001)
    try:
        from system.global_exception_handler import install_global_exception_handlers

        install_global_exception_handlers()
    except Exception:
        pass
    _boot_milestone_t0 = time.monotonic()
    _apply_launcher_desktop_env_early()
    # Step 0 — evict stale port listeners + SHM before singleton lock or heavy boot
    from system.identity.process_orchestrator import os_surface_cleanse

    if not is_v32_dual_port_mode():
        os_surface_cleanse()
    else:
        os_surface_cleanse(api_port=_engine_cli.port)
    _boot_milestone("os_surface_cleanse")
    # Step 1 — kernel singleton lock (SO_REUSEADDR for TIME_WAIT recovery)
    enforce_absolute_socket_singleton()
    os.environ.setdefault("IG_NON_BLOCKING_BOOT", "1")
    harness_ticks = _parse_harness_ticks()
    daemon_cycle_sec = _parse_daemon_cycle()
    isolated_track = _parse_isolated_track()
    if harness_ticks is not None and harness_ticks <= 0:
        print("FAIL-CLOSED: --test-harness-ticks must be a positive integer", file=sys.stderr)
        sys.exit(15)
    if daemon_cycle_sec is not None and daemon_cycle_sec <= 0:
        print("FAIL-CLOSED: --daemon-cycle must be a positive integer", file=sys.stderr)
        sys.exit(15)
    if harness_ticks is not None and daemon_cycle_sec is not None:
        print(
            "FAIL-CLOSED: --test-harness-ticks and --daemon-cycle are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(15)

    parallel_dual = os.environ.get("IG_PARALLEL_DUAL", "0").strip() == "1"
    unified_engine = os.environ.get("IG_UNIFIED_ENGINE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    orchestrator_child = os.environ.get("IG_ORCHESTRATOR_CHILD", "").strip() == "1"
    if (
        daemon_cycle_sec is not None
        and isolated_track is None
        and parallel_dual
        and not unified_engine
        and not orchestrator_child
        and harness_ticks is None
    ):
        from system.identity.process_orchestrator import launch_dual_tracks_detached
        from system.paths import project_root

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        live_pid, shadow_pid = launch_dual_tracks_detached(cycle_sec=int(daemon_cycle_sec))
        try:
            from system.shutdown_cleanup import clear_manual_stop

            clear_manual_stop()
        except Exception as exc:
            _log_engine(f"parallel launch: clear_manual_stop failed: {type(exc).__name__}: {exc}")
        from system.identity.process_orchestrator import run_parallel_supervisor_forever

        run_parallel_supervisor_forever(
            cycle_sec=int(daemon_cycle_sec),
            live_pid=int(live_pid),
            shadow_pid=int(shadow_pid),
        )

    if daemon_cycle_sec is not None and isolated_track is not None and not orchestrator_child:
        from system.identity.process_orchestrator import (
            configure_live_vanguard_env,
            configure_shadow_simulator_env,
        )

        if isolated_track == "live":
            configure_live_vanguard_env(cycle_sec=int(daemon_cycle_sec))
        else:
            configure_shadow_simulator_env(cycle_sec=int(daemon_cycle_sec))
    elif (
        daemon_cycle_sec is not None
        and isolated_track is None
        and unified_engine
        and not orchestrator_child
    ):
        from system.unified_engine import configure_unified_engine_env

        configure_unified_engine_env(cycle_sec=int(daemon_cycle_sec), api_port=_api_port())
    elif daemon_cycle_sec is not None and isolated_track is None and orchestrator_child:
        pass
    elif daemon_cycle_sec is not None and isolated_track is None:
        from system.daemon_cycle_kernel import configure_daemon_cycle_env, detach_daemon_runtime
        from system.paths import project_root

        os.environ.setdefault("IG_AGENT_ROOT", str(project_root()))
        configure_daemon_cycle_env(daemon_cycle_sec)
        detach_daemon_runtime(log_path=Path("/tmp/ig_agent.live.log"))

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
    if _engine_cli.dual_port_mode:
        reapply_engine_cli_env(_engine_cli)
    _force_launcher_non_blocking_boot()
    _boot_milestone("dotenv_prepared")

    from runtime.app_mode import apply_app_mode_to_environ
    from runtime.session_lock import acquire_session_lock

    try:
        apply_app_mode_to_environ()
    except ValueError as exc:
        _log_engine(f"APP_MODE boot failed: {exc}")
        sys.exit(EXIT_CONFIG)
    except RuntimeError as exc:
        _log_engine(f"APP_MODE LIVE gate failed: {exc}")
        sys.exit(EXIT_CONFIG)

    lock_ok, lock_msg = acquire_session_lock()
    if not lock_ok:
        _log_engine(f"session lock refused: {lock_msg}")
        sys.exit(EXIT_INSTANCE)

    _boot_milestone("session_lock")
    _start_pre_bind_watchdog()

    from system.boot.non_blocking_bootstrap import non_blocking_boot_enabled

    _defer_pre_bind = _defer_heavy_pre_bind_work()

    try:
        from system.agent_execution_mode import ensure_production_execution_armed_on_boot

        ensure_production_execution_armed_on_boot()
    except Exception as exc:
        _log_engine(f"production execution arming failed: {type(exc).__name__}: {exc}")

    _boot_milestone("execution_armed")

    if os.environ.get("IG_ORCHESTRATOR_CHILD", "").strip() != "1" or isolated_track is not None:
        if not non_blocking_boot_enabled() and not _defer_pre_bind:
            try:
                from system.guard.kernel_interceptor import install_kernel_interceptor

                install_kernel_interceptor()
            except Exception as exc:
                _log_engine(f"KernelInterceptor bootstrap failed: {type(exc).__name__}: {exc}")
        else:
            _log_engine("non_blocking_boot: KernelInterceptor deferred to post-bind")

    if harness_ticks is not None:
        from system.test_harness.runner import configure_harness_env

        # Watchdog relaunches use IG_AGENT_SKIP_DEPLOY_CHECK=1 (see start_agent_background.sh)
        configure_harness_env(harness_ticks)
    from system.apex_runtime_mode import apply_runtime_mode_to_environ

    apply_runtime_mode_to_environ()
    profile = apply_node_profile_to_environ()
    stamp_process_boot_start()
    _boot_milestone("profile_stamped")

    if not _defer_pre_bind:
        try:
            from system.boot.boot_orchestrator import init_boot_pipeline

            init_boot_pipeline()
        except Exception:
            pass

    from system.app_identity import APP_DISPLAY_NAME, APP_VERSION_LABEL
    from system.boot.exceptions import Gate1FatalError
    from system.boot.gate1_preflight import run_gate1_preflight
    from system.system_state import BootPhase, get_system_state

    _desktop_fast_bind = os.environ.get("IG_APEX_DESKTOP", "").strip() == "1"
    _harness_entry = harness_ticks is not None
    _daemon_entry = daemon_cycle_sec is not None

    if not _harness_entry and not _daemon_entry:
        atexit.register(lambda: _force_cleanup_port(_api_port()))
        _rotate_oversized_logs()
    _log_engine(
        f"=== {APP_DISPLAY_NAME} {APP_VERSION_LABEL} full restart "
        f"(node={profile.kind} api=:{profile.api_port}) ==="
    )
    if not _harness_entry and not _daemon_entry:
        if not _defer_pre_bind:
            _exchange_rollover_emergence_pause()
            if not non_blocking_boot_enabled() and not _skip_v32_singleton_lock():
                _purge_workspace_bytecode()
            else:
                _log_engine("non_blocking_boot: bytecode purge deferred to post-bind")
        else:
            _log_engine("non_blocking_boot: rollover/purge/startup cleanup deferred to post-bind")
    if _desktop_fast_bind and not _defer_pre_bind:
        try:
            from apex.ipc_bridge import start_ipc_bridge_daemon
            from apex.microkernel import start_microkernel
            from execution.atomic_gateway import set_monitoring_mode

            set_monitoring_mode(False)
            start_ipc_bridge_daemon()
            start_microkernel(workers_only=True)
            _log_engine("Apex desktop: IPC bridge + micro-kernel armed (workers only)")
        except Exception as exc:
            _log_engine(f"Apex desktop bootstrap failed: {type(exc).__name__}: {exc}")

    if not _defer_pre_bind:
        _pre_startup_cleanup()

    _boot_milestone("pre_runtime")

    try:
        from runtime.pid_registry import write_agent_pid

        written = write_agent_pid()
        if written:
            _log_engine(f"pid_registry: agent.pid mirrored → {', '.join(written)}")
    except Exception as exc:
        _log_engine(f"pid_registry: write skipped ({type(exc).__name__})")

    boot_ctx = None
    if _desktop_fast_bind or non_blocking_boot_enabled() or _defer_pre_bind:
        _log_engine(
            "Non-blocking boot: deferring Gate1 to post-bind lifespan (fast API bind)"
        )
    else:
        try:
            boot_ctx = run_gate1_preflight()
        except Gate1FatalError as exc:
            sys.exit(exc.exit_code)

    runtime = AgentRuntime(boot_context=boot_ctx)
    _install_signal_handlers(runtime)
    try:
        if harness_ticks is not None:
            raise SystemExit(runtime.run_harness(tick_count=harness_ticks))
        if daemon_cycle_sec is not None:
            raise SystemExit(runtime.run_daemon_cycle(interval_sec=float(daemon_cycle_sec)))
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
