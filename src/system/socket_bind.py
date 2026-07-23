"""API listen-socket helpers — SO_REUSEADDR + guarded SO_REUSEPORT (v34).

Intent: allow supervised twin binds (v32 dual-port) without TIME_WAIT stalls.
SO_REUSEPORT is best-effort — skipped when the platform constant is missing or
``setsockopt`` rejects the option (common on some macOS builds).
"""

from __future__ import annotations

import socket
from typing import Any

from system.engine_log import log_engine


def _set_reuseport(sock: socket.socket) -> bool:
    opt = getattr(socket, "SO_REUSEPORT", None)
    if opt is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, opt, 1)
        return True
    except OSError:
        return False


def create_listen_socket(
    host: str,
    port: int,
    *,
    backlog: int = 2048,
) -> tuple[socket.socket, bool]:
    """Create a bound/listening TCP socket with reuse options."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reuseport = _set_reuseport(sock)
    sock.bind((host, int(port)))
    sock.listen(int(backlog))
    return sock, reuseport


def build_uvicorn_config(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    log_level: str = "info",
    **kwargs: Any,
) -> tuple[Any, socket.socket | None]:
    """
    Return ``(uvicorn.Config, hold_socket)``.

    When reuse-port bind succeeds, ``Config`` is created with ``fd=`` so Uvicorn
    inherits the prepared socket. Caller must keep ``hold_socket`` alive for the
    server lifetime.
    """
    import uvicorn

    try:
        sock, reuseport = create_listen_socket(host, int(port))
        if reuseport:
            log_engine(
                f"socket_bind: SO_REUSEPORT armed on {host}:{port} "
                "(target <5ms bind jitter — best-effort, not guaranteed)"
            )
        else:
            log_engine(
                f"socket_bind: SO_REUSEADDR only on {host}:{port} "
                "(SO_REUSEPORT unavailable or rejected)"
            )
        cfg = uvicorn.Config(app, fd=sock.fileno(), log_level=log_level, **kwargs)
        return cfg, sock
    except OSError as exc:
        log_engine(
            f"socket_bind: reuse-port pre-bind failed ({type(exc).__name__}) "
            f"— falling back to uvicorn host/port"
        )
        cfg = uvicorn.Config(
            app, host=host, port=int(port), log_level=log_level, **kwargs
        )
        return cfg, None
