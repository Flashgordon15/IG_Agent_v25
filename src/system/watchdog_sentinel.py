"""
WatchdogSelfHealer — passive :9090 health pinger with sub-500ms microkernel recovery.

Kills hung sidecar PIDs, evicts stale IPC sockets, hot-reloads Python microkernel quietly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from system.engine_log import log_engine

_PING_INTERVAL_SEC = 5.0
_HEALTH_TIMEOUT_SEC = 2.0
_RECOVERY_COOLDOWN_SEC = 30.0
_BOOT_GRACE_SEC = 60.0

_lock = threading.RLock()
_healer: WatchdogSelfHealer | None = None


def _resolve_health_url() -> str:
    port = int(os.environ.get("IG_API_PORT", "9090"))
    return f"http://127.0.0.1:{port}/api/health"


def _resolve_ipc_sockets() -> list[str]:
    paths: list[str] = []
    try:
        from system.paths import data_dir

        paths.append(str(data_dir() / "apex_ipc.sock"))
    except Exception:
        pass
    try:
        from system.node_profile import get_node_profile

        paths.append(str(get_node_profile().data_dir / "apex_ipc.sock"))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[2]
    paths.append(str(root / "src" / "data" / "apex_ipc.sock"))
    return paths


class WatchdogSelfHealer:
    """Independent lightweight service — 5s REST health ping + staged reclamation."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_recovery_mono = 0.0
        self._recovery_count = 0
        self._healthy = True
        self._started_mono = time.monotonic()

    def start(self) -> None:
        with _lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="watchdog-self-healer",
                daemon=True,
            )
            self._thread.start()
            log_engine(f"WatchdogSelfHealer: passive :{port} monitor armed (5s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def stats(self) -> dict[str, Any]:
        return {
            "healthy": self._healthy,
            "recovery_count": self._recovery_count,
            "ping_interval_sec": _PING_INTERVAL_SEC,
        }

    def _loop(self) -> None:
        if self._stop.wait(_BOOT_GRACE_SEC):
            return
        while not self._stop.is_set():
            try:
                ok = self._ping_health()
                self._healthy = ok
                if not ok:
                    self._execute_recovery()
                else:
                    from apex.operational_transparency import clear_recovery_active

                    clear_recovery_active()
            except Exception as exc:
                log_engine(
                    f"WatchdogSelfHealer loop error: {type(exc).__name__}: {exc}"
                )
            if self._stop.wait(_PING_INTERVAL_SEC):
                break

    def _ping_health(self) -> bool:
        if time.monotonic() - self._started_mono < _BOOT_GRACE_SEC:
            return True
        url = _resolve_health_url()
        try:
            res = requests.get(url, timeout=_HEALTH_TIMEOUT_SEC)
            if not (200 <= int(res.status_code) < 500):
                return False
            try:
                payload = res.json()
                if isinstance(payload, dict) and payload.get("bootstrapping") is True:
                    return True
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _execute_recovery(self) -> None:
        if time.monotonic() - self._started_mono < _BOOT_GRACE_SEC:
            return
        now = time.monotonic()
        if now - self._last_recovery_mono < _RECOVERY_COOLDOWN_SEC:
            return
        self._last_recovery_mono = now
        self._recovery_count += 1

        from apex.operational_transparency import set_recovery_active

        set_recovery_active(True)
        log_engine("WatchdogSelfHealer: RECOVERY_ACTIVE — staging reclamation pass")
        t0 = time.monotonic()

        port = int(os.environ.get("IG_API_PORT", "9090"))
        my_pid = os.getpid()
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-tiTCP", f"{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in (result.stdout or "").strip().splitlines():
                pid = line.strip()
                if not pid.isdigit() or int(pid) == my_pid:
                    continue
                subprocess.run(["/bin/kill", "-TERM", pid], timeout=2)
                time.sleep(0.2)
                subprocess.run(["/bin/kill", "-9", pid], timeout=2)
                log_engine(f"WatchdogSelfHealer: evicted hung PID {pid} on :{port}")
        except Exception as exc:
            log_engine(f"WatchdogSelfHealer PID eviction: {type(exc).__name__}: {exc}")

        ipc_running = False
        try:
            from apex.ipc_bridge import get_ipc_bridge

            ipc_running = bool(get_ipc_bridge().stats().get("running"))
        except Exception:
            pass

        if port == 9090 and not ipc_running:
            for sock in _resolve_ipc_sockets():
                try:
                    if os.path.exists(sock):
                        os.remove(sock)
                        log_engine(f"WatchdogSelfHealer: removed stale socket {sock}")
                except OSError:
                    pass

        root = Path(__file__).resolve().parents[2]
        py = os.environ.get("PYTHON", sys.executable)
        venv_py = root / ".venv" / "bin" / "python3"
        if venv_py.is_file():
            py = str(venv_py)
        env = {
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "IG_API_PORT": str(port),
            "IG_NODE_PROFILE": "shadow",
            "IG_APEX_DESKTOP": "1",
            "IG_APEX_DAEMON": "1",
        }
        try:
            subprocess.Popen(
                [py, str(root / "src" / "main.py")],
                cwd=str(root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            log_engine(
                f"WatchdogSelfHealer microkernel spawn failed: {type(exc).__name__}: {exc}"
            )

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        log_engine(
            f"WatchdogSelfHealer: microkernel hot-reload dispatched ({elapsed_ms:.0f}ms)"
        )


def get_watchdog_self_healer() -> WatchdogSelfHealer:
    global _healer
    with _lock:
        if _healer is None:
            _healer = WatchdogSelfHealer()
        return _healer


def start_watchdog_self_healer() -> WatchdogSelfHealer:
    healer = get_watchdog_self_healer()
    healer.start()
    return healer


def reset_watchdog_self_healer_for_tests() -> None:
    global _healer
    with _lock:
        if _healer is not None:
            _healer.stop()
        _healer = None
    from apex.operational_transparency import clear_recovery_active

    clear_recovery_active()
