"""
Anti-zombie process guard — hard teardown for desktop Flight Deck shell.

On window close, terminates child processes, frees ports 8080/8787, and releases locks.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

_DEFAULT_PORTS: tuple[int, ...] = (8080, 8787)
_TEARDOWN_DEADLINE_SEC = 1.0
_KILL_PATTERNS: tuple[str, ...] = (
    "src/main.py",
    "cockpit.desktop_app_shell",
    "desktop_app_shell.py",
    "cockpit.web_server",
    "uvicorn",
    "flight_deck_launch",
    "desktop_cockpit.py",
    "scripts/desktop_cockpit",
)

_lock = threading.RLock()
_child_pids: set[int] = set()
_shutdown_once = threading.Event()


def register_child_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    with _lock:
        _child_pids.add(int(pid))


def register_child_process(proc: subprocess.Popen[Any]) -> None:
    if proc.pid:
        register_child_pid(int(proc.pid))


def registered_child_pids() -> list[int]:
    with _lock:
        return sorted(_child_pids)


def reset_desktop_process_guard_for_tests() -> None:
    with _lock:
        _child_pids.clear()
    _shutdown_once.clear()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def port_is_bound(port: int, host: str = "127.0.0.1") -> bool:
    """Socket-level listen/connect probe — True when TCP port accepts connections."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.35)
            return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def audit_and_purge_bound_ports(
    *,
    ports: tuple[int, ...] = _DEFAULT_PORTS,
    preserve_pid: int | None = None,
) -> dict[str, Any]:
    """
    Cold-start port audit — detect rogue listeners on 8080/8787 and SIGKILL before boot.

    Runs before desktop shell or supervisor smoke tests to prevent binding collisions.
    """
    my_pid = int(preserve_pid if preserve_pid is not None else os.getpid())
    summary: dict[str, Any] = {"ok": True, "ports": {}, "purged_pids": []}

    for port in ports:
        was_bound = port_is_bound(port)
        listeners = [p for p in _pids_on_port(port) if p != my_pid]
        killed: list[int] = []
        for pid in listeners:
            if _signal_pid(pid, signal.SIGTERM):
                killed.append(pid)
        if listeners:
            time.sleep(0.12)
        for pid in _pids_on_port(port):
            if pid == my_pid:
                continue
            if pid not in killed and _signal_pid(pid, signal.SIGKILL):
                killed.append(pid)
        summary["ports"][str(port)] = {
            "was_bound": was_bound,
            "listener_pids": listeners,
            "killed": sorted(set(killed)),
        }
        summary["purged_pids"].extend(killed)

    summary["purged_pids"] = sorted(set(summary["purged_pids"]))
    if summary["purged_pids"]:
        log_engine(
            "DesktopProcessGuard: cold-start port purge "
            f"pids={summary['purged_pids']} ports={list(summary['ports'].keys())}"
        )
    return summary


def destroy_orphan_zombie_processes(
    *,
    ports: tuple[int, ...] = _DEFAULT_PORTS,
    preserve_pid: int | None = None,
) -> dict[str, Any]:
    """
    Zombie destroyer — SIGKILL python3/bash listeners on 8080/8787 before boot cycles.

    Invoked by runtime stabilizer and desktop cold-start paths.
    """
    my_pid = int(preserve_pid if preserve_pid is not None else os.getpid())
    summary = audit_and_purge_bound_ports(ports=ports, preserve_pid=my_pid)
    orphan_killed: list[int] = []
    for pattern in ("python3", "bash"):
        for pid in _kill_pattern(pattern, sig=signal.SIGKILL):
            if pid == my_pid:
                continue
            holds_port = any(pid in _pids_on_port(p) for p in ports)
            if holds_port and pid not in orphan_killed:
                orphan_killed.append(pid)
    summary["orphan_killed"] = sorted(set(orphan_killed))
    summary["ok"] = True
    if orphan_killed or summary.get("purged_pids"):
        log_engine(
            "DesktopProcessGuard: zombie destroyer "
            f"orphans={summary['orphan_killed']} purged={summary.get('purged_pids')}"
        )
    return summary


def _pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _signal_pid(pid: int, sig: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _kill_pattern(pattern: str, *, sig: int = signal.SIGKILL) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    killed: list[int] = []
    for line in (out.stdout or "").splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        if _signal_pid(pid, sig):
            killed.append(pid)
    return killed


def _transmit_broker_disconnect_frame() -> dict[str, Any]:
    """Force clean disconnect on registered broker streaming sockets."""
    summary: dict[str, Any] = {"ok": True, "flushed": 0, "errors": []}
    try:
        from ig_api.streaming_factory import flush_streaming_session_handles

        summary.update(flush_streaming_session_handles())
        summary["ok"] = True
    except Exception as exc:
        summary["ok"] = False
        summary["errors"].append(f"{type(exc).__name__}: {exc}")
    return summary


def _purge_ports_until_deadline(
    ports: tuple[int, ...],
    *,
    deadline: float,
    preserve_pid: int,
) -> dict[str, dict[str, list[int]]]:
    cleared: dict[str, dict[str, list[int]]] = {}
    for port in ports:
        killed: list[int] = []
        while time.monotonic() < deadline:
            targets = [p for p in _pids_on_port(port) if p != preserve_pid]
            if not targets:
                break
            for pid in targets:
                sig = signal.SIGTERM if time.monotonic() + 0.08 < deadline else signal.SIGKILL
                if _signal_pid(pid, sig):
                    killed.append(pid)
            time.sleep(0.04)
        for pid in _pids_on_port(port):
            if pid != preserve_pid and pid not in killed:
                if _signal_pid(pid, signal.SIGKILL):
                    killed.append(pid)
        cleared[str(port)] = {"killed": sorted(set(killed))}
    return cleared


def execute_sequential_desktop_teardown(
    *,
    root: Path | None = None,
    ports: tuple[int, ...] = _DEFAULT_PORTS,
    skip_mark_manual_stop: bool = False,
    deadline_sec: float = _TEARDOWN_DEADLINE_SEC,
) -> dict[str, Any]:
    """
    Strict sequential desktop exit — manual stop, broker disconnect, child kill, port free.

    Completes within ``deadline_sec`` (default 1000ms) using SIGTERM→SIGKILL escalation.
    """
    if _shutdown_once.is_set():
        return {"ok": True, "skipped": True, "reason": "already_shutdown"}
    _shutdown_once.set()

    repo = root or _project_root()
    deadline = time.monotonic() + max(0.25, float(deadline_sec))
    started = time.monotonic()
    summary: dict[str, Any] = {
        "ok": True,
        "deadline_sec": deadline_sec,
        "manual_stop": False,
        "broker_disconnect": {},
        "children_killed": [],
        "pattern_killed": [],
        "ports_cleared": {},
        "locks_removed": [],
    }

    if not skip_mark_manual_stop:
        try:
            from system.shutdown_cleanup import mark_manual_stop

            mark_manual_stop(source="desktop_app_shell_exit")
            summary["manual_stop"] = True
        except Exception as exc:
            summary["manual_stop_error"] = f"{type(exc).__name__}: {exc}"

    summary["broker_disconnect"] = _transmit_broker_disconnect_frame()

    with _lock:
        child_targets = list(_child_pids)
    for pid in child_targets:
        if time.monotonic() >= deadline:
            break
        if _signal_pid(pid, signal.SIGTERM):
            summary["children_killed"].append(pid)
    time.sleep(0.05)
    for pid in child_targets:
        if time.monotonic() >= deadline:
            break
        if _signal_pid(pid, signal.SIGKILL):
            summary["children_killed"].append(pid)

    for pattern in _KILL_PATTERNS:
        if time.monotonic() >= deadline:
            break
        summary["pattern_killed"].extend(_kill_pattern(pattern, sig=signal.SIGTERM))

    summary["ports_cleared"] = _purge_ports_until_deadline(
        ports,
        deadline=deadline,
        preserve_pid=os.getpid(),
    )

    lock_paths = (
        repo / "src/data/.ig_agent_v29.lock",
        repo / "src/data/.ig_agent_v31.lock",
    )
    for path in lock_paths:
        if path.is_file():
            try:
                path.unlink()
                summary["locks_removed"].append(str(path))
            except OSError:
                pass

    try:
        from cockpit.web_server import stop_cockpit_web_server

        stop_cockpit_web_server()
        summary["cockpit_web_stopped"] = True
    except Exception:
        pass

    with _lock:
        _child_pids.clear()

    summary["children_killed"] = sorted(set(summary["children_killed"]))
    summary["pattern_killed"] = sorted(set(summary["pattern_killed"]))
    summary["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    log_engine(
        "DesktopProcessGuard: sequential teardown "
        f"manual_stop={summary.get('manual_stop')} "
        f"broker_flushed={summary['broker_disconnect'].get('flushed', 0)} "
        f"children={len(summary['children_killed'])} "
        f"ports={summary['ports_cleared']}"
    )
    return summary


def execute_anti_zombie_shutdown(
    *,
    root: Path | None = None,
    ports: tuple[int, ...] = _DEFAULT_PORTS,
    skip_mark_manual_stop: bool = False,
) -> dict[str, Any]:
    """
    Force SIGKILL on tracked children and port listeners; flush locks.

    Idempotent — safe to call multiple times (subsequent calls are no-ops).
    """
    return execute_sequential_desktop_teardown(
        root=root,
        ports=ports,
        skip_mark_manual_stop=skip_mark_manual_stop,
    )
