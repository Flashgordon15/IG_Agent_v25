"""Lightweight operational monitor — PID, port 8080, on_loop_tick hooks (§17)."""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ai.paths import sentinel_diagnostics_path
from system.paths import data_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _agent_pid() -> int | None:
    lock = os.environ.get("IG_AGENT_PID")
    if lock and str(lock).isdigit():
        return int(lock)
    lock_path = data_dir() / ".ig_agent_v25.lock"
    if lock_path.exists():
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
            pid = int(raw.split()[0]) if raw else 0
            return pid if pid > 0 else None
        except (ValueError, OSError):
            return None
    return os.getpid()


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass
class TickHealth:
    epic: str
    loop_error: bool = False
    stream_disconnected: bool = False
    quote_stale: bool = False

    @property
    def unhealthy(self) -> bool:
        return self.loop_error or self.stream_disconnected or self.quote_stale


@dataclass
class SystemMonitor:
    """Background tracker for agent health and loop tick hooks."""

    port: int = 8080
    host: str = "127.0.0.1"
    poll_interval_sec: float = 2.0
    agent_pid: int | None = field(default_factory=_agent_pid)
    repair_engine: Any | None = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _tick_streak: dict[str, int] = field(default_factory=dict, repr=False)
    _lines: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.repair_engine is None:
            from ai.operational.auto_repair import AutoRepairEngine

            self.repair_engine = AutoRepairEngine(monitor=self)

    def _bootstrap_healthy(self) -> bool:
        self.agent_pid = _agent_pid()
        return pid_alive(self.agent_pid) and port_open(self.host, self.port)

    def _loops_healthy(self) -> bool:
        if not self._tick_streak:
            return True
        return all(v < 3 for v in self._tick_streak.values())

    def _maybe_clear_boot_lock(self) -> None:
        """Clear stale dead-drop freeze when agent + :8080 are live and ticks are clean."""
        if self.repair_engine is None or not self._bootstrap_healthy():
            return
        if not self._loops_healthy():
            return
        if self.repair_engine.is_frozen():
            self.repair_engine.clear_safety_freeze(reason="bootstrap_healthy")

    def on_loop_tick(
        self,
        epic: str,
        *,
        loop_error: bool = False,
        stream_disconnected: bool = False,
        quote_stale: bool = False,
    ) -> dict[str, Any]:
        """Hook invoked once per trading-loop iteration (§17)."""
        health = TickHealth(
            epic=str(epic or "unknown"),
            loop_error=loop_error,
            stream_disconnected=stream_disconnected,
            quote_stale=quote_stale,
        )
        key = health.epic
        if health.unhealthy:
            self._tick_streak[key] = self._tick_streak.get(key, 0) + 1
        else:
            self._tick_streak[key] = 0
            self._maybe_clear_boot_lock()

        event = {
            "ts": _utc_now(),
            "type": "loop_tick",
            "epic": key,
            "unhealthy": health.unhealthy,
            "loop_error": loop_error,
            "stream_disconnected": stream_disconnected,
            "quote_stale": quote_stale,
            "consecutive_unhealthy": self._tick_streak[key],
            "agent_pid": self.agent_pid,
            "pid_alive": pid_alive(self.agent_pid),
            "port_open": port_open(self.host, self.port),
        }
        self._append_diagnostic(event)

        if self._tick_streak[key] >= 3 and health.unhealthy:
            if self.repair_engine is not None:
                dead = self.repair_engine.execute_dead_drop(
                    epic=key,
                    reason="consecutive_unhealthy_ticks",
                    loop_error=loop_error,
                    stream_disconnected=stream_disconnected,
                )
                event["dead_drop"] = dead

        return event

    def snapshot(self) -> dict[str, Any]:
        return {
            "ts": _utc_now(),
            "agent_pid": self.agent_pid,
            "pid_alive": pid_alive(self.agent_pid),
            "port": self.port,
            "port_open": port_open(self.host, self.port),
            "tick_streak": dict(self._tick_streak),
            "safety_freeze": self.repair_engine.is_frozen()
            if self.repair_engine
            else False,
        }

    def _append_diagnostic(self, event: dict[str, Any]) -> None:
        self._lines.append(event)
        if len(self._lines) > 200:
            self._lines = self._lines[-200:]
        path = sentinel_diagnostics_path()
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def recent_diagnostics(self, limit: int = 80) -> list[dict[str, Any]]:
        cap = max(1, min(limit, 200))
        return list(self._lines[-cap:])

    def _background_pass(self) -> None:
        self._maybe_clear_boot_lock()
        snap = self.snapshot()
        self._append_diagnostic({"ts": snap["ts"], "type": "monitor_pass", **snap})
        try:
            from ai.operational.profiler import get_operational_profiler

            get_operational_profiler().maybe_investigate_all()
        except Exception:
            pass
        if self.repair_engine is not None:
            self.repair_engine.check_approved_proposals()

    def run_background(
        self,
        stop_event: threading.Event | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """Start lightweight polling loop (PID + port 8080)."""
        if self._thread and self._thread.is_alive():
            return
        stop = stop_event or self._stop

        def _loop() -> None:
            self._maybe_clear_boot_lock()
            while not stop.is_set():
                self.agent_pid = _agent_pid()
                self._background_pass()
                if on_tick:
                    try:
                        on_tick()
                    except Exception:
                        pass
                stop.wait(self.poll_interval_sec)

        self._thread = threading.Thread(
            target=_loop, name="sentinel-monitor", daemon=True
        )
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)


# Process-wide sandbox monitor (optional wiring from agent bootstrap).
_default_monitor: SystemMonitor | None = None


def get_system_monitor() -> SystemMonitor:
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = SystemMonitor()
        _default_monitor._maybe_clear_boot_lock()
    return _default_monitor
