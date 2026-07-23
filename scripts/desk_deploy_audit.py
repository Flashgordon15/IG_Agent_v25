#!/usr/bin/env python3
"""
Desk deploy audit — read-only pre-deploy checks for Trading Desk.

Used by ``scripts/desk_deploy.sh audit`` and unit tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("APP_MODE", "DEMO")
os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")

API_BASE = "http://127.0.0.1:8080"
SESSION_CONFIG = "config/config_v31_demo_throughput.json"


@dataclass
class ProcessInfo:
    name: str
    pid: int | None = None
    alive: bool = False
    cmd: str = ""


@dataclass
class AuditReport:
    ok: bool = True
    broker_open: int = 0
    flat: bool = True
    deploy_allowed: bool = False
    session_state: str = "unknown"
    manual_stop_active: bool = False
    port_8080_listening: bool = False
    processes: list[ProcessInfo] = field(default_factory=list)
    wrappers: dict[str, Any] = field(default_factory=dict)
    position_manager: dict[str, Any] = field(default_factory=dict)
    market: dict[str, Any] = field(default_factory=dict)
    supervise_loop_running: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "broker_open": self.broker_open,
            "flat": self.flat,
            "deploy_allowed": self.deploy_allowed,
            "session_state": self.session_state,
            "manual_stop_active": self.manual_stop_active,
            "port_8080_listening": self.port_8080_listening,
            "processes": [
                {"name": p.name, "pid": p.pid, "alive": p.alive, "cmd": p.cmd}
                for p in self.processes
            ],
            "wrappers": self.wrappers,
            "position_manager": self.position_manager,
            "market": self.market,
            "supervise_loop_running": self.supervise_loop_running,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
        }


def _http_get(path: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _pgrep(pattern: str) -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-fl", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[tuple[int, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            out.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
    return out


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_listening(port: int = 8080) -> bool:
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool((result.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _load_status_payload(path: Path) -> dict[str, Any]:
    """Parse a JSON object or JSONL (last valid object wins) status/audit file."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("empty status file")
    try:
        raw = json.loads(text)
        if isinstance(raw, dict):
            return raw
    except json.JSONDecodeError:
        pass
    # JSONL / concatenated objects — take the last parseable dict line.
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last = obj
    if last is None:
        raise ValueError(f"no JSON object in {path.name}")
    return last


def _wrapper_status(path: Path, *, stale_sec: float = 60.0) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "stale": True, "age_sec": None}
    try:
        raw = _load_status_payload(path)
        age = time.time() - float(raw.get("ts") or 0)
        return {
            "present": True,
            "stale": age > stale_sec,
            "age_sec": round(age, 1),
            "raw": raw,
        }
    except (OSError, ValueError, TypeError) as exc:
        return {"present": True, "stale": True, "error": str(exc)}


def _market_snapshot() -> dict[str, Any]:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from intelligence.premium_overnight import in_rollover_lock

        now = datetime.now(ZoneInfo("Europe/London"))
        rollover = in_rollover_lock(now=now)
        return {
            "rollover_lock": rollover,
            "local_time_bst": now.strftime("%H:%M %Z"),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_audit(*, force_supervised: bool = False) -> AuditReport:
    report = AuditReport()

    report.port_8080_listening = _port_listening(8080)

    try:
        from system.shutdown_cleanup import manual_stop_active

        report.manual_stop_active = bool(manual_stop_active())
    except Exception:
        report.manual_stop_active = False

    report.market = _market_snapshot()

    # Broker opens via API (preferred) or REST fallback
    try:
        live = _http_get("/api/positions/live", timeout=6.0)
        report.broker_open = int(live.get("count") or 0)
        unm = int(live.get("unmonitored") or 0)
        if unm > 0:
            report.warnings.append(f"{unm} unmonitored position(s)")
        if live.get("stale"):
            report.warnings.append("positions/live cache stale")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        report.issues.append(f"positions/live unreachable: {exc}")
        try:
            from system.config_loader import get_config
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import get_shared_rest_client

            cred = try_load_credentials()
            if cred.ok and cred.credentials:
                rest = get_shared_rest_client(cred.credentials)
                report.broker_open = len(list(rest.open_positions(budget_priority=True) or []))
        except Exception as rest_exc:
            report.issues.append(f"REST fallback failed: {rest_exc}")

    report.flat = report.broker_open == 0

    main_hits = _pgrep("src/main.py")
    trade_hits = _pgrep("runtime.trade_support_wrapper")
    desk_hits = _pgrep("runtime.desk_support_wrapper")
    supervise_hits = _pgrep("manage_live_positions.py --supervise")

    report.supervise_loop_running = bool(supervise_hits)

    def _pick(hits: list[tuple[int, str]], label: str) -> ProcessInfo:
        if not hits:
            return ProcessInfo(name=label, alive=False)
        pid, cmd = hits[0]
        return ProcessInfo(name=label, pid=pid, alive=_pid_alive(pid), cmd=cmd)

    report.processes = [
        _pick(main_hits, "main"),
        _pick(trade_hits, "trade_support"),
        _pick(desk_hits, "desk_support"),
    ]

    try:
        from system.paths import data_dir

        root = data_dir()
    except Exception:
        root = ROOT / "src" / "data"

    report.wrappers = {
        "trade_support": _wrapper_status(root / "trade_support_status.json"),
        "desk_support_audit": _wrapper_status(root / "desk_support_audit.jsonl", stale_sec=120.0),
    }

    if report.port_8080_listening:
        try:
            report.position_manager = _http_get("/api/position_manager/status", timeout=4.0)
            last = report.position_manager.get("last_report") or {}
            if last.get("error") == "tick_in_progress":
                report.warnings.append("OPM last tick skipped (tick_in_progress)")
            tick_count = int(report.position_manager.get("tick_count") or 0)
            if tick_count == 0 and report.broker_open > 0:
                report.warnings.append("OPM tick_count=0 with open positions")
        except Exception as exc:
            report.warnings.append(f"position_manager/status: {exc}")

    # Session state classification
    if report.broker_open > 0:
        report.session_state = "active_session"
    elif report.port_8080_listening and not report.manual_stop_active:
        report.session_state = "deploy_window"
    elif report.manual_stop_active:
        report.session_state = "manual_stop"
    else:
        report.session_state = "dev"

    for proc in report.processes:
        if proc.name in ("trade_support", "desk_support") and not proc.alive:
            report.issues.append(f"{proc.name} wrapper not running")
        if proc.name == "trade_support" and proc.alive:
            ts = report.wrappers.get("trade_support") or {}
            if ts.get("stale"):
                report.warnings.append("trade_support status file stale")

    if force_supervised and not report.supervise_loop_running:
        report.issues.append("--force-supervised requires supervise loop running")

    report.deploy_allowed = report.flat or (
        force_supervised and report.supervise_loop_running
    )
    if not report.flat and not force_supervised:
        report.warnings.append(
            f"deploy blocked: broker_open={report.broker_open} (use --force-supervised if supervised)"
        )

    report.ok = not report.issues
    return report


def print_audit(report: AuditReport) -> None:
    print("=== DESK DEPLOY AUDIT ===")
    print(f"session_state:     {report.session_state}")
    print(f"broker_open:       {report.broker_open} (flat={report.flat})")
    print(f"deploy_allowed:    {report.deploy_allowed}")
    print(f"manual_stop:       {report.manual_stop_active}")
    print(f"port 8080:         {report.port_8080_listening}")
    print(f"supervise_loop:    {report.supervise_loop_running}")
    for proc in report.processes:
        pid = proc.pid or "-"
        print(f"process {proc.name:14} pid={pid} alive={proc.alive}")
    if report.market:
        print(
            f"market:            rollover={report.market.get('rollover_lock')} "
            f"({report.market.get('local_time_bst', '')})"
        )
    pm = report.position_manager
    if pm:
        print(
            f"OPM:               active={pm.get('active')} tick_count={pm.get('tick_count')} "
            f"last_error={pm.get('last_error') or '-'}"
        )
    for w in report.warnings:
        print(f"  WARN {w}")
    for i in report.issues:
        print(f"  ISSUE {i}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Desk deploy audit")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--force-supervised", action="store_true")
    args = parser.parse_args()
    report = run_audit(force_supervised=args.force_supervised)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_audit(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
