#!/usr/bin/env python3
"""launchd-safe agent start — resolves python and execs src/main.py (no Desktop bash)."""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

_BOOT_GRACE_SEC = 15.0
_API_HOST = "127.0.0.1"
_API_PORT = 8080
_NETWORK_INTERFACES_PLIST = Path(
    "/Library/Preferences/SystemConfiguration/NetworkInterfaces.plist"
)


def _agent_root() -> Path:
    env = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def resolve_python(root: Path) -> Path:
    candidates = [
        root / ".venv" / "bin" / "python3",
        root / "venv" / "bin" / "python3",
        Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"),
        Path("/opt/homebrew/bin/python3"),
        Path("/usr/local/bin/python3"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return Path(sys.executable)


def check_port_available(host: str = _API_HOST, port: int = _API_PORT) -> bool:
    """Return True when nothing is accepting TCP on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex((host, port)) != 0


def network_interfaces_ready() -> bool:
    """Match launchd PathState gate — wait for OS network driver config."""
    return _NETWORK_INTERFACES_PLIST.is_file()


def boot_grace_sleep_if_needed(*, grace_sec: float = _BOOT_GRACE_SEC) -> None:
    """
    Post-reboot grace: slow Wi-Fi / stale port bind can fail the first local check.

    Sleeps once before exec so IG auth and uvicorn bind happen after the stack settles.
    """
    port_ok = check_port_available()
    net_ok = network_interfaces_ready()
    if port_ok and net_ok:
        return

    reasons: list[str] = []
    if not port_ok:
        reasons.append(f"port {_API_PORT} busy")
    if not net_ok:
        reasons.append("network interfaces not ready")
    print(
        f"start_agent_launchd: boot grace — sleeping {grace_sec:.0f}s "
        f"({', '.join(reasons) or 'waiting for stack'})",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(max(0.0, float(grace_sec)))


def main() -> int:
    root = _agent_root()
    py = resolve_python(root)
    main_py = root / "src" / "main.py"
    if not main_py.is_file():
        print(f"start_agent_launchd: missing {main_py}", file=sys.stderr)
        return 1

    boot_grace_sleep_if_needed()

    os.chdir(root)
    os.environ["IG_AGENT_ROOT"] = str(root)
    os.environ.setdefault("IG_AGENT_FROM_LAUNCHER", "1")
    os.environ.setdefault("IG_AGENT_SKIP_DEPLOY_CHECK", "1")
    src_path = str(root / "src")
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = src_path if not existing else f"{src_path}:{existing}"

    os.execv(str(py), [str(py), str(main_py)])


if __name__ == "__main__":
    raise SystemExit(main())
