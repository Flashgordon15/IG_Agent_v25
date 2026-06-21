"""Reclaim API bind ports — evict stale listeners before Gate 1 / preflight."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

from system.engine_log import log_engine

_PROTECTED_PORTS = (8080, 8787)


def _resolve_killable_pid(raw_pid: str | int) -> int | None:
    clean = str(raw_pid).strip()
    if not clean.isdigit():
        return None
    pid = int(clean)
    if pid == os.getpid() or pid == os.getppid():
        return None
    return pid


def should_protect_production_port(port: int) -> bool:
    """Shadow/testbed profiles must not evict production :8080 / :8787."""
    if port not in _PROTECTED_PORTS:
        return False
    try:
        from system.node_profile import get_node_profile

        profile = get_node_profile()
        if profile.is_testbed or profile.is_shadow:
            return True
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("port_eviction", exc)
    protect = os.environ.get("IG_APEX_PROTECT_PRODUCTION_PORTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not protect:
        return False
    try:
        from system.node_profile import is_shadow_node

        if is_shadow_node():
            return True
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("port_eviction", exc)
    return False


def _evict_listener_pid(target_pid: int, bind_port: int) -> bool:
    """SIGTERM first, brief wait, then SIGKILL — never kill self."""
    if _resolve_killable_pid(target_pid) is None:
        return False
    try:
        os.kill(target_pid, signal.SIGTERM)
        log_engine(f"port eviction: SIGTERM PID {target_pid} on :{bind_port}")
    except ProcessLookupError:
        return False
    except Exception as exc:
        log_engine(
            f"port eviction: SIGTERM failed PID {target_pid}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    deadline = time.time() + 2.5
    while time.time() < deadline:
        try:
            os.kill(target_pid, 0)
            time.sleep(0.15)
        except ProcessLookupError:
            return True
        except Exception:
            break
    try:
        os.kill(target_pid, signal.SIGKILL)
        log_engine(f"port eviction: SIGKILL PID {target_pid} on :{bind_port}")
        return True
    except ProcessLookupError:
        return True
    except Exception as exc:
        log_engine(
            f"port eviction: SIGKILL failed PID {target_pid}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def reclaim_api_port(port: int | None = None, *, force: bool = False) -> list[int]:
    """
    SIGKILL any process listening on *port* (never self) and drop stale instance lock.

    Returns PIDs that were signalled. Skips pytest unless *force* is True.
    """
    from system.boot.preflight_helpers import resolve_api_port
    from system.instance_lock import lock_path as instance_lock_path

    bind_port = resolve_api_port() if port is None else int(port)
    if os.environ.get("IG_AGENT_PYTEST") == "1" and not force:
        return []

    if not force and should_protect_production_port(bind_port):
        log_engine(
            f"port eviction: shadow profile — refusing to evict production port {bind_port}"
        )
        return []

    killed: list[int] = []
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", f":{bind_port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pid_str in result.stdout.strip().splitlines():
            target_pid = _resolve_killable_pid(pid_str)
            if target_pid is None:
                continue
            if _evict_listener_pid(target_pid, bind_port):
                killed.append(target_pid)
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("port_eviction", exc)

    try:
        instance_lock_path().unlink(missing_ok=True)
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("port_eviction", exc)
    return killed


def wait_for_port_free(
    port: int | None = None,
    *,
    timeout_sec: float = 3.0,
    poll_sec: float = 0.3,
) -> bool:
    """Poll until *port* is bindable or *timeout_sec* elapses."""
    from system.boot.preflight_helpers import check_port_available, resolve_api_port

    bind_port = resolve_api_port() if port is None else int(port)
    deadline = time.time() + max(0.1, float(timeout_sec))
    while time.time() < deadline:
        if check_port_available(bind_port):
            return True
        time.sleep(max(0.05, float(poll_sec)))
    return check_port_available(bind_port)


def port_held_by_current_process(port: int) -> bool:
    """True when this PID (or child) already LISTENs on *port* — post-uvicorn Gate 1."""
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", f":{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        own = {str(os.getpid()), str(os.getppid())}
        for pid_str in result.stdout.strip().splitlines():
            if pid_str.strip() in own:
                return True
    except Exception as exc:
        from system.guard.runtime_guard import log_guarded_exception

        log_guarded_exception("port_eviction", exc)
    return False


def reclaim_and_wait(port: int | None = None, *, force: bool = False) -> bool:
    """Evict listeners then confirm the port is free to bind."""
    bind_port = port
    if bind_port is None:
        from system.boot.preflight_helpers import resolve_api_port

        bind_port = resolve_api_port()
    reclaim_api_port(bind_port, force=force)
    if wait_for_port_free(bind_port):
        return True
    return port_held_by_current_process(bind_port)
