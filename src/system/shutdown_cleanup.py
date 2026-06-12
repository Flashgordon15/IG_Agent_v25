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
_MANUAL_STOP_FILE = data_dir() / "state" / "manual_stop.json"
_MANUAL_STOP_MAX_AGE_SEC = 600.0


def reset_shutdown_verify_state() -> None:
    """Clear stale verify snapshot so dashboard polling cannot read a prior run."""
    path = data_dir() / "state" / "last_shutdown_verify.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "status": "pending",
                    "checks": [],
                    "issues": [],
                    "ts": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def mark_manual_stop(*, source: str = "dashboard") -> None:
    """Signal watchdog/launchd not to auto-restart after deliberate Stop Agent."""
    try:
        reset_shutdown_verify_state()
        _MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MANUAL_STOP_FILE.write_text(
            json.dumps({"ts": time.time(), "source": source}),
            encoding="utf-8",
        )
        log_engine(f"manual_stop: flagged (source={source})")
        try:
            from system.overnight_supervision import clear_overnight_armed

            clear_overnight_armed()
        except Exception:
            pass
    except Exception as e:
        log_engine(f"manual_stop: flag failed: {type(e).__name__}: {e}")


def clear_manual_stop() -> None:
    try:
        _MANUAL_STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def manual_stop_active(*, max_age_sec: float = _MANUAL_STOP_MAX_AGE_SEC) -> bool:
    if not _MANUAL_STOP_FILE.is_file():
        return False
    try:
        raw = json.loads(_MANUAL_STOP_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("ts") or 0)
        return age >= 0 and age < max_age_sec
    except Exception:
        return True


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


def perform_shutdown_cleanup(
    *, source: str = "shutdown", skip_port_cleanup: bool = False
) -> None:
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
        from system.telegram_alerts import stop_hourly_executive_telegram_scheduler
        from system.telegram_notifier import stop_telegram_heartbeat

        stop_hourly_executive_telegram_scheduler()
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
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
        # Preserve launchd only when actually loaded; otherwise stop standalone watchdog.
        stop_watchdog(preserve_launchd=launchd_wd)
        if launchd_wd:
            log_engine(
                "shutdown cleanup: launchd supervision preserved (manual_stop respected)"
            )
        else:
            log_engine("shutdown cleanup: standalone watchdog stopped (no launchd)")
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
        from system.instance_lock import force_release_instance_lock

        force_release_instance_lock()
    except Exception as e:
        log_engine(f"shutdown cleanup: lock release error (continuing): {e}")

    if not skip_port_cleanup:
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


def stopped_verification_checks(issues: list[str]) -> list[dict[str, object]]:
    """Structured checklist matching scripts/confirm_stopped.py."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
    except Exception:
        launchd_wd = False

    mapping = [
        ("No main.py process", "main.py still running"),
        (
            "Launchd supervision preserved" if launchd_wd else "No watchdog process",
            "watchdog.sh still running",
        ),
        ("Port 8080 free", "port 8080 still bound"),
        ("No instance lock", "instance lock file present"),
        (
            "Watchdog supervising (launchd)" if launchd_wd else "No watchdog.pid",
            "watchdog.pid present",
        ),
    ]
    return [
        {
            "label": label,
            "ok": issue_key not in issues,
            "detail": issue_key if issue_key in issues else "",
        }
        for label, issue_key in mapping
    ]


def post_cleanup_shutdown_checks(*, exclude_pid: int) -> list[dict[str, object]]:
    """Immediate checks after perform_shutdown_cleanup (before process exit)."""
    checks: list[dict[str, object]] = []
    pids = _list_main_py_pids()
    only_self = not pids or pids == [exclude_pid]
    checks.append(
        {
            "label": "Cleanup completed",
            "ok": True,
            "detail": "streams, IG session, DB checkpoint",
        }
    )
    checks.append(
        {
            "label": "Trading process exiting",
            "ok": only_self,
            "detail": (
                f"pid {exclude_pid} shutting down"
                if exclude_pid in pids
                else "no unexpected main.py"
            ),
        }
    )
    try:
        from api.agent_health import _watchdog_active
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
        watchdog_active = _watchdog_active()
    except Exception:
        launchd_wd = False
        watchdog_active = True
    if launchd_wd:
        checks.append(
            {
                "label": "Launchd supervision preserved",
                "ok": True,
                "detail": "watchdog job still loaded",
            }
        )
    else:
        checks.append(
            {
                "label": "Watchdog stopped",
                "ok": not watchdog_active,
                "detail": "" if not watchdog_active else "watchdog still active",
            }
        )
    port_bound = _port_bound()
    checks.append(
        {
            "label": "Port 8080 released",
            "ok": not port_bound,
            "detail": "" if not port_bound else "port still bound",
        }
    )
    lock = data_dir() / ".ig_agent_v25.lock"
    checks.append(
        {
            "label": "Instance lock released",
            "ok": not lock.is_file(),
            "detail": "" if not lock.is_file() else "lock file present",
        }
    )
    wd_pid = data_dir() / "watchdog.pid"
    try:
        from system.overnight_supervision import launchd_watchdog_active

        launchd_wd = launchd_watchdog_active()
    except Exception:
        launchd_wd = False
    if launchd_wd:
        checks.append(
            {
                "label": "Watchdog PID (launchd)",
                "ok": True,
                "detail": "expected while supervision loaded",
            }
        )
    else:
        checks.append(
            {
                "label": "Watchdog PID cleared",
                "ok": not wd_pid.is_file(),
                "detail": "" if not wd_pid.is_file() else "watchdog.pid present",
            }
        )
    return checks


def spawn_post_shutdown_verifier(parent_pid: int) -> None:
    """Detached process waits for agent exit then serves verify JSON on :8081."""
    import sys

    from system.paths import project_root

    root = project_root()
    script = root / "scripts" / "shutdown_verify_server.py"
    if not script.is_file():
        log_engine("shutdown verify: script missing — skipped")
        return
    try:
        log_path = data_dir() / "logs" / "shutdown_verify_spawn.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as spawn_log:
            spawn_log.write(
                f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} | spawn parent={parent_pid}\n"
            )
        err_log = data_dir() / "logs" / "shutdown_verify_stderr.log"
        err_fh = err_log.open("a", encoding="utf-8")
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--parent-pid",
                str(parent_pid),
            ],
            cwd=str(root),
            env={
                **dict(os.environ),
                "PYTHONPATH": str(root / "src"),
            },
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
        )
        log_engine(f"shutdown verify: spawned post-exit checker (parent={parent_pid})")
    except Exception as e:
        log_engine(f"shutdown verify: spawn failed: {type(e).__name__}: {e}")


def repair_stale_watchdog_after_stop() -> tuple[bool, str]:
    """Kill orphaned watchdog.sh when launchd is not supervising."""
    try:
        from system.overnight_supervision import launchd_watchdog_active

        if launchd_watchdog_active():
            return True, "launchd supervision active — no repair needed"
    except Exception:
        pass
    try:
        from api.agent_health import stop_watchdog

        stop_watchdog(preserve_launchd=False)
    except Exception as e:
        return False, f"watchdog repair failed: {type(e).__name__}: {e}"
    ok, issues = agent_fully_stopped()
    if ok:
        return True, "watchdog stopped and pid cleared"
    return False, ", ".join(issues) if issues else "watchdog still present"


def agent_fully_stopped(
    *, preserve_launchd_supervision: bool = True
) -> tuple[bool, list[str]]:
    """Return (ok, issues) when the trading agent process has fully exited."""
    issues: list[str] = []

    if _list_main_py_pids():
        issues.append("main.py still running")

    launchd_wd = False
    if preserve_launchd_supervision:
        try:
            from system.overnight_supervision import launchd_watchdog_active

            launchd_wd = launchd_watchdog_active()
        except Exception:
            pass

    if not launchd_wd:
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
    if wd_pid.is_file() and not launchd_wd:
        issues.append("watchdog.pid present")

    return (len(issues) == 0, issues)
