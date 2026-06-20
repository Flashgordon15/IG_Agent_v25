"""
Apex Monolith - in-process IPC tick bridge (Unix domain socket).

Replaces renderer reliance on exposed :8080/:8787 WebSocket ports when
IG_APEX_DESKTOP=1. Subscribes to snapshot_store and fans out newline JSON.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_SOCKET_NAME = "apex_ipc.sock"


def _default_socket_path() -> Path:
    import os

    name = os.environ.get("IG_APEX_IPC_SOCKET", _SOCKET_NAME).strip() or _SOCKET_NAME
    return data_dir() / name
_MAX_CLIENTS = 8
_BACKLOG = 4


class ApexIpcBridge:
    """Thread-safe Unix-socket fan-out for dashboard ticks."""

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path or _default_socket_path()
        self._lock = threading.RLock()
        self._clients: list[socket.socket] = []
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._unsubscribe: Any | None = None
        self._running = False
        self._ticks_sent = 0

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self._socket_path))
        self._server.listen(_BACKLOG)
        mode = 0o660 if os.environ.get("IG_APEX_DESKTOP", "").strip() == "1" else 0o600
        os.chmod(self._socket_path, mode)
        if not self._probe_listener():
            log_engine("Apex IPC: stale socket detected — rebinding listener")
            try:
                self._server.close()
            except OSError:
                pass
            try:
                if self._socket_path.exists():
                    self._socket_path.unlink()
            except OSError:
                pass
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self._socket_path))
            self._server.listen(_BACKLOG)
            os.chmod(self._socket_path, mode)
        self._thread = threading.Thread(
            target=self._accept_loop, name="apex-ipc-accept", daemon=True
        )
        self._thread.start()
        from api.snapshot_store import get_tick, subscribe

        self._unsubscribe = subscribe(self._on_tick)
        try:
            self.broadcast(get_tick())
        except Exception:
            pass
        log_engine(f"Apex IPC bridge listening on {self._socket_path}")

    def _probe_listener(self) -> bool:
        """Return True when the bound UDS accepts local connections."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.3)
            probe.connect(str(self._socket_path))
            return True
        except OSError:
            return False
        finally:
            try:
                probe.close()
            except OSError:
                pass

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._unsubscribe is not None:
                try:
                    self._unsubscribe()
                except Exception:
                    pass
                self._unsubscribe = None
            for client in list(self._clients):
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        assert self._server is not None
        while self._running:
            try:
                self._server.settimeout(1.0)
                conn, _addr = self._server.accept()
                conn.setblocking(True)
                with self._lock:
                    if len(self._clients) >= _MAX_CLIENTS:
                        conn.close()
                        continue
                    self._clients.append(conn)
                log_engine("Apex IPC: client connected")
            except TimeoutError:
                continue
            except OSError:
                if self._running:
                    time.sleep(0.1)
                break

    def _on_tick(self, tick: dict[str, Any]) -> None:
        self.broadcast(tick)

    def broadcast(self, payload: dict[str, Any]) -> None:
        try:
            from apex.operational_transparency import record_bridge_handshake

            record_bridge_handshake("ipc")
        except Exception:
            pass
        line = (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode(
            "utf-8"
        )
        dead: list[socket.socket] = []
        with self._lock:
            for client in self._clients:
                try:
                    client.sendall(line)
                except OSError:
                    dead.append(client)
            for client in dead:
                try:
                    self._clients.remove(client)
                    client.close()
                except (OSError, ValueError):
                    pass
            self._ticks_sent += 1
        try:
            from apex.system_monitor import record_ipc_handshake

            with self._lock:
                clients = len(self._clients)
            record_ipc_handshake(clients=clients)
        except Exception:
            pass

    def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan-out typed IPC envelopes (warmup progress, status, ticks)."""
        body = {"type": str(event_type), **payload}
        self.broadcast(body)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "socket_path": str(self._socket_path),
                "clients": len(self._clients),
                "ticks_sent": self._ticks_sent,
                "running": self._running,
            }


_bridge: ApexIpcBridge | None = None
_bridge_lock = threading.Lock()


def get_ipc_bridge() -> ApexIpcBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = ApexIpcBridge()
        return _bridge


def start_ipc_bridge_daemon() -> ApexIpcBridge:
    bridge = get_ipc_bridge()
    bridge.start()
    try:
        from apex.avionics_story import start_weekend_heartbeat_daemon

        start_weekend_heartbeat_daemon()
    except Exception:
        pass
    return bridge


def broadcast_warmup_event(snapshot: dict[str, Any]) -> None:
    """Publish array-compile progress to all IPC subscribers."""
    try:
        get_ipc_bridge().broadcast_event("warmup", snapshot)
    except Exception:
        pass


def broadcast_ledger_event(row: dict[str, Any]) -> None:
    """Publish Worker D / execution fill rows to dashboard avionics ledger."""
    try:
        from datetime import datetime, timezone

        body = dict(row)
        if "ts_iso" not in body:
            ts = body.get("ts")
            if isinstance(ts, (int, float)):
                body["ts_iso"] = (
                    datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z"
                )
        get_ipc_bridge().broadcast_event("ledger", body)
    except Exception:
        pass


def broadcast_story_event(entry: dict[str, Any]) -> None:
    """Publish avionics lifecycle narrative lines to dashboard storytelling terminal."""
    try:
        get_ipc_bridge().broadcast_event("story", entry)
    except Exception:
        pass


def stop_ipc_bridge() -> None:
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            _bridge.stop()
            _bridge = None
