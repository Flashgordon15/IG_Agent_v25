#!/usr/bin/env python3
"""HTTP server: confirm agent fully stopped after dashboard shutdown."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VERIFY_PORT = 8081
VERIFY_PATH = "/shutdown-verify"
LOG_FILE = ROOT / "src" / "data" / "logs" / "shutdown_verify.log"
STATE_FILE = ROOT / "src" / "data" / "state" / "last_shutdown_verify.json"


def _log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n"
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _write_state(payload: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def _free_listen_port(port: int) -> None:
    try:
        pids = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for pid_str in pids.splitlines():
        try:
            os.kill(int(pid_str.strip()), signal.SIGTERM)
            _log(f"freed stale listener on :{port} pid={pid_str.strip()}")
        except (ProcessLookupError, ValueError, PermissionError):
            pass


def _wait_for_parent_exit(parent_pid: int, *, timeout_sec: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            _log(f"parent pid {parent_pid} exited")
            return True
        except PermissionError:
            _log(f"parent pid {parent_pid} not accessible — treat as exited")
            return True
        time.sleep(0.1)
    _log(f"parent pid {parent_pid} still alive after {timeout_sec:.0f}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post-shutdown verification HTTP server"
    )
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()

    payload: dict = {
        "ok": False,
        "status": "waiting",
        "checks": [],
        "issues": [],
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != VERIFY_PATH:
                self.send_response(404)
                self.end_headers()
                return
            with lock:
                body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    _write_state(payload)

    _free_listen_port(VERIFY_PORT)

    try:
        # Bind all interfaces so localhost (IPv4/IPv6) and 127.0.0.1 both reach the verifier.
        server = HTTPServer(("0.0.0.0", VERIFY_PORT), Handler)
        server.timeout = 0.5
    except OSError as e:
        _log(f"verify server bind failed: {type(e).__name__}: {e}")
        return 1

    serve_deadline = [time.monotonic() + 90.0]

    def _serve() -> None:
        while time.monotonic() < serve_deadline[0]:
            server.handle_request()

    thread = threading.Thread(target=_serve, name="shutdown-verify-http", daemon=True)
    thread.start()
    _log(f"verify server listening on :{VERIFY_PORT} parent_pid={args.parent_pid}")

    _wait_for_parent_exit(args.parent_pid)

    with lock:
        payload["status"] = "checking"

    from system.shutdown_cleanup import agent_fully_stopped, stopped_verification_checks

    ok = False
    issues: list[str] = ["verification timeout"]
    for attempt in range(60):
        ok, issues = agent_fully_stopped()
        if ok:
            _log(f"fully stopped confirmed on attempt {attempt + 1}")
            break
        time.sleep(0.25)
    else:
        _log(f"verify failed: {', '.join(issues)}")

    with lock:
        payload.update(
            {
                "ok": ok,
                "status": "done",
                "checks": stopped_verification_checks(issues),
                "issues": issues,
            }
        )
        final = dict(payload)

    _write_state(final)
    _log(f"verify complete ok={ok}")
    # Keep answering dashboard polls after the agent process has exited (match manual_stop TTL).
    serve_deadline[0] = time.monotonic() + 600.0
    thread.join(timeout=605.0)
    server.server_close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
