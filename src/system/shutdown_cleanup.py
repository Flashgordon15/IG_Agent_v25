"""Graceful agent teardown and startup/stop deployment verification."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from system.engine_log import log_engine
from system.paths import data_dir

_cleanup_done = False


def reset_shutdown_cleanup_for_tests() -> None:
    """Allow repeated cleanup in pytest."""
    global _cleanup_done
    _cleanup_done = False


def kill_other_agent_processes(
    *,
    exclude_pid: int | None = None,
    sigkill_survivors: bool = True,
    wait_sec: float = 3.0,
    log_label: str = "agent cleanup",
) -> list[int]:
    """SIGTERM (then optional SIGKILL) any other src/main.py processes."""
    own = exclude_pid if exclude_pid is not None else os.getpid()
    killed: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue
            if pid == own:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                log_engine(f"{log_label}: SIGTERM orphan agent PID {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                log_engine(f"{log_label}: could not SIGTERM PID {pid}: {e}")
    except Exception as e:
        log_engine(f"{log_label}: pgrep failed: {e}")
        return killed

    if not killed or not sigkill_survivors:
        return killed

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        alive = []
        for pid in killed:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if not alive:
            break
        time.sleep(0.2)

    for pid in killed:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            log_engine(f"{log_label}: SIGKILL orphan agent PID {pid}")
        except ProcessLookupError:
            pass
        except Exception:
            pass
    return killed


def perform_shutdown_cleanup(*, source: str = "shutdown") -> None:
    """Full teardown so Stop Agent leaves no rogue sessions or duplicate processes."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    log_engine(f"shutdown cleanup: begin (source={source})")

    try:
        from api.agent_control import stop_trading

        stop_trading()
    except Exception as e:
        log_engine(f"shutdown cleanup: stop_trading error (continuing): {e}")

    try:
        from runtime.agent_bootstrap import stop_ig_position_sync, stop_market_stream

        stop_market_stream()
        stop_ig_position_sync()
        log_engine("shutdown cleanup: market stream and position sync stopped")
    except Exception as e:
        log_engine(f"shutdown cleanup: stream/sync stop error (continuing): {e}")

    try:
        from system.trading_health_monitor import stop_trading_health_monitor

        stop_trading_health_monitor()
    except Exception:
        pass

    try:
        from system.telegram_notifier import stop_telegram_heartbeat

        stop_telegram_heartbeat()
    except Exception:
        pass

    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        store = LearningStore(str(get_config().learning_db))
        store.checkpoint()
        store.close()
        log_engine("shutdown cleanup: learning store checkpointed")
    except Exception as e:
        log_engine(
            f"shutdown cleanup: learning store checkpoint error (continuing): {e}"
        )

    try:
        from system.ig_rest_session import shutdown_shared_ig_session

        shutdown_shared_ig_session()
    except Exception as e:
        log_engine(f"shutdown cleanup: IG session end error (continuing): {e}")

    try:
        from api.agent_health import stop_watchdog

        stop_watchdog()
        log_engine("shutdown cleanup: watchdog stopped")
    except Exception as e:
        log_engine(f"shutdown cleanup: watchdog stop error (continuing): {e}")

    orphans = kill_other_agent_processes(
        exclude_pid=os.getpid(), sigkill_survivors=True
    )
    if orphans:
        log_engine(
            f"shutdown cleanup: cleared {len(orphans)} duplicate agent process(es)"
        )

    try:
        from system.instance_lock import release_instance_lock

        release_instance_lock()
    except Exception as e:
        log_engine(f"shutdown cleanup: lock release error (continuing): {e}")

    try:
        import main as _main

        _main._force_cleanup_port()
    except Exception as e:
        log_engine(f"shutdown cleanup: port cleanup error (continuing): {e}")

    log_engine("shutdown cleanup: complete")


def _list_main_py_pids() -> list[int]:
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "src/main.py"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return pids
        for pid_str in result.stdout.strip().splitlines():
            try:
                pids.append(int(pid_str.strip()))
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def _port_bound(port: int = 8080) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _instance_lock_holder_pid() -> int | None:
    lock = data_dir() / ".ig_agent_v25.lock"
    if not lock.is_file():
        return None
    try:
        raw = lock.read_text(encoding="utf-8").strip()
        holder = int(raw.split()[0]) if raw else 0
    except (ValueError, OSError):
        return None
    if holder <= 0:
        return None
    try:
        os.kill(holder, 0)
        return holder
    except OSError:
        return None


def _fetch_api_health(timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8080/api/health", timeout=timeout
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        return None


def agent_fully_started(
    *,
    max_gate_age_sec: float = 120.0,
    stream_log_within_min: float = 10.0,
    require_trading_healthy: bool = True,
) -> tuple[bool, list[str]]:
    """Return (ok, issues) when startup completed and the agent is trading-ready."""
    issues: list[str] = []

    pids = _list_main_py_pids()
    if not pids:
        issues.append("main.py not running")
    elif len(pids) > 1:
        issues.append(f"duplicate main.py processes ({len(pids)})")

    if not _port_bound():
        issues.append("port 8080 not bound")

    lock_pid = _instance_lock_holder_pid()
    if lock_pid is None:
        issues.append("instance lock missing or stale")
    elif pids and lock_pid not in pids:
        issues.append("instance lock held by non-agent pid")

    try:
        from api.agent_health import _watchdog_active

        if not _watchdog_active():
            issues.append("watchdog not running")
    except Exception:
        issues.append("watchdog status unknown")

    health = _fetch_api_health()
    if health is None:
        issues.append("cannot reach /api/health")
    else:
        if not health.get("trading_loops_running"):
            issues.append("trading loops not running")
        if require_trading_healthy and not health.get("trading_healthy"):
            detail = ", ".join(health.get("issues") or []) or "unhealthy"
            issues.append(f"trading not healthy ({detail})")
        gate_age = health.get("last_gate_check_age_sec")
        if gate_age is None:
            issues.append("no gate check activity")
        elif float(gate_age) > max_gate_age_sec:
            issues.append(f"gate check stale ({int(float(gate_age))}s)")

    try:
        from system.pre_flight_checks import check_startup_stream_gate_log

        stream_result = check_startup_stream_gate_log(
            within_minutes=stream_log_within_min
        )
        if not stream_result.passed:
            issues.append("stream_ready not in recent engine.log")
    except Exception:
        issues.append("stream_ready log check failed")

    return (len(issues) == 0, issues)


def agent_fully_stopped() -> tuple[bool, list[str]]:
    """Return (ok, issues) when no agent/watchdog/listener remains."""
    issues: list[str] = []

    if _list_main_py_pids():
        issues.append("main.py still running")

    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", "scripts/watchdog.sh"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            issues.append("watchdog.sh still running")
    except Exception:
        pass

    if _port_bound():
        issues.append("port 8080 still bound")

    lock = data_dir() / ".ig_agent_v25.lock"
    if lock.is_file():
        issues.append("instance lock file present")

    wd_pid = data_dir() / "watchdog.pid"
    if wd_pid.is_file():
        issues.append("watchdog.pid present")

    return (len(issues) == 0, issues)
