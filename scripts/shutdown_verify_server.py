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


def _log_paths() -> tuple[Path, Path]:
    from system.paths import data_dir, logs_dir

    return (
        logs_dir() / "shutdown_verify.log",
        data_dir() / "state" / "last_shutdown_verify.json",
    )


def _log(msg: str) -> None:
    log_file, _ = _log_paths()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _write_state(payload: dict) -> None:
    _, state_file = _log_paths()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload), encoding="utf-8")


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

    from system.shutdown_cleanup import ensure_supervision_utilities_executable

    ensure_supervision_utilities_executable()

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

    from system.shutdown_cleanup import (
        agent_fully_stopped,
        repair_stale_watchdog_after_stop,
        stopped_verification_checks,
        _list_main_py_pids,
        _port_bound,
    )

    def _supervision_fields() -> dict:
        from system.overnight_supervision import overnight_supervision_summary
        from system.shutdown_cleanup import manual_stop_active
        from system.supervision_monitor import evaluate_supervision_drift

        drift = evaluate_supervision_drift()
        summary = overnight_supervision_summary()
        warnings = list(drift.get("warnings") or [])
        if manual_stop_active() and "manual_stop_active_agent_down" not in warnings:
            warnings.append("manual_stop_active_agent_down")
        return {
            "supervision_drift_ok": bool(drift.get("ok")),
            "supervision_drift": drift,
            "supervision_warnings": warnings,
            "overnight_supervision": summary,
            "overnight_armed": bool(summary.get("overnight_armed")),
        }

    ok = False
    issues: list[str] = ["verification timeout"]
    watchdog_repaired = False

    def _try_watchdog_repair(current_issues: list[str]) -> bool:
        nonlocal ok, issues, watchdog_repaired
        stale_watchdog = any(
            i in current_issues
            for i in ("watchdog.sh still running", "watchdog.pid present")
        )
        if not stale_watchdog or watchdog_repaired:
            return False
        repaired, detail = repair_stale_watchdog_after_stop()
        watchdog_repaired = True
        _log(f"watchdog repair: ok={repaired} ({detail})")
        if not repaired:
            return False
        ok, issues = agent_fully_stopped()
        return ok

    # Closed :8080 with no agent process — expected after dashboard Stop.
    if not _port_bound() and not _list_main_py_pids():
        ok, issues = agent_fully_stopped()
        if ok:
            _log("fully stopped — port 8080 closed, no main.py")
        elif _try_watchdog_repair(issues):
            _log("fully stopped after fast-path watchdog repair")

    if not ok:
        for attempt in range(60):
            ok, issues = agent_fully_stopped()
            if ok:
                _log(f"fully stopped confirmed on attempt {attempt + 1}")
                break
            if _try_watchdog_repair(issues):
                _log("fully stopped after watchdog repair")
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
                **_supervision_fields(),
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
