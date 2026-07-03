#!/usr/bin/env python3
"""
Chaos restart proof — full tear-down, clean start, trade-ready verification, repeat.

Uses the operator anti-zombie protocol:
  1. mark_manual_stop
  2. agent_kill.sh (graceful TERM → port free)
  3. agent_start.sh (headless — no Electron/GUI)
  4. Poll until execution plane is live-trade capable
  5. Gap and repeat

Usage:
  PYTHONPATH=src python3 scripts/chaos_restart_prove.py
  PYTHONPATH=src python3 scripts/chaos_restart_prove.py --cycles 3 --gap-sec 30
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_PORT = int(os.environ.get("IG_API_PORT", "8080") or 8080)
LOG_DIR = ROOT / "logs" / "chaos_restart"
REPORT_PATH = LOG_DIR / "chaos_restart_report.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _log(msg: str) -> None:
    line = f"[{_utc()}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "chaos_restart.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _fetch_json(path: str, *, port: int, timeout: float = 4.0) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IG-ChaosRestart/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _port_free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return False
    except OSError:
        return True


def _open_positions() -> int:
    for rel in ("src/data/runtime_state.json", "src/data/state/runtime_state.json"):
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            pos = data.get("open_positions") or data.get("positions") or []
            return len(pos) if isinstance(pos, list) else int(pos or 0)
        except Exception:
            pass
    return 0


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
    merged = {**os.environ, **(env or {})}
    merged["PYTHONPATH"] = str(ROOT / "src")
    _log(f"RUN: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=merged,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if proc.stdout.strip():
            _log(f"stdout: {proc.stdout.strip()[:500]}")
        if proc.stderr.strip():
            _log(f"stderr: {proc.stderr.strip()[:500]}")
        return int(proc.returncode)
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT after {timeout}s: {' '.join(cmd)}")
        return 124


@dataclass
class PollSnapshot:
    ts: str
    elapsed_sec: float
    port_live: bool
    boot_tier: str
    execution_loop_active: bool
    routes_armed: int
    feeds_fresh: int
    trade_ready: bool
    iron_gauge_sealed: bool
    iron_gauge_tier: str
    orchestrator_primed: bool
    orchestrator_healthy: bool
    blockers: list[str] = field(default_factory=list)
    accept: bool = False
    accept_reason: str = ""


def evaluate_trade_ready(*, port: int) -> PollSnapshot:
    from cockpit.launcher_post_ready import post_ready_execution_acceptable

    t0 = time.monotonic()
    hl = _fetch_json("/api/health_light", port=port) or {}
    boot = _fetch_json("/api/boot_status", port=port) or {}
    gauge = _fetch_json("/api/iron_gauge", port=port) or {}
    orch = _fetch_json("/api/orchestrator_state", port=port) or {}
    iron = _fetch_json("/api/iron_cage_status", port=port) or {}

    boot_tier = "red"
    try:
        from system.boot.iron_gauge import evaluate_startup_tier

        boot_tier = str(evaluate_startup_tier(port=port, in_process=False)).lower()
    except Exception:
        if hl.get("agent_online") is not False and exec_active:
            boot_tier = "amber"

    rs = hl.get("routing_state") or {}
    armed = int(rs.get("armed") or 0)
    feeds = int(((hl.get("data_feeds") or {}).get("hub") or {}).get("fresh_count") or 0)
    exec_active = bool(hl.get("execution_loop_active"))
    trade_ready = bool(boot.get("trade_ready") or iron.get("trade_ready"))

    accept, reason = post_ready_execution_acceptable(
        health_light=hl, boot_status=boot, boot_tier=boot_tier
    )
    strict_ok = exec_active and armed > 0 and (trade_ready or boot_tier == "green")
    if strict_ok and not accept:
        accept, reason = True, "strict_execution_plane"

    blockers: list[str] = []
    if not exec_active:
        blockers.append("execution_loop_inactive")
    if armed < 1:
        blockers.append("routes_unarmed")
    if not trade_ready and boot_tier not in ("green", "amber"):
        blockers.append(f"boot_tier={boot_tier}")
    blockers.extend(list(gauge.get("blockers") or [])[:8])
    blockers.extend(list(iron.get("blockers") or [])[:8])

    return PollSnapshot(
        ts=_utc(),
        elapsed_sec=round(time.monotonic() - t0, 3),
        port_live=not _port_free(port),
        boot_tier=boot_tier,
        execution_loop_active=exec_active,
        routes_armed=armed,
        feeds_fresh=feeds,
        trade_ready=trade_ready,
        iron_gauge_sealed=bool(gauge.get("sealed")),
        iron_gauge_tier=str(gauge.get("tier") or ""),
        orchestrator_primed=bool(orch.get("primed")),
        orchestrator_healthy=bool(orch.get("healthy")),
        blockers=blockers[:16],
        accept=bool(accept and exec_active and armed > 0),
        accept_reason=reason,
    )


@dataclass
class CycleResult:
    cycle: int
    started_at: str
    ended_at: str
    duration_sec: float
    teardown_ok: bool
    start_ok: bool
    trade_ready: bool
    final: dict[str, Any] = field(default_factory=dict)
    polls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def teardown(*, port: int) -> bool:
    py = ROOT / ".venv/bin/python3"
    if not py.is_file():
        py = Path(sys.executable)
    rc_mark = _run(
        [str(py), "-c", "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='chaos_restart')"],
        timeout=30,
    )
    if rc_mark != 0:
        _log("WARN: mark_manual_stop non-zero")
    rc_kill = _run(["/bin/bash", str(ROOT / "macos/launcher/agent_kill.sh")], timeout=180)
    if rc_kill != 0:
        _log(f"ERROR: agent_kill exit {rc_kill}")
        return False
    if not _port_free(port):
        _log(f"ERROR: port {port} still bound after agent_kill")
        return False
    return True


def start_agent(*, port: int, cycle: int) -> bool:
    env = {
        "IG_API_PORT": str(port),
        "APP_MODE": os.environ.get("APP_MODE", "DEMO"),
        "IG_AGENT_CONFIG": os.environ.get(
            "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
        ),
        "LAUNCHER_SKIP_TESTS": "1",
        "LAUNCHER_SKIP_ELECTRON_GUI": "1",
        "LAUNCHER_SKIP_GUI_SERVER": "1",
        "LAUNCHER_SKIP_NPM_DEV": "1",
        "LAUNCHER_WAIT_FOR_LOCK": "1",
    }
    if cycle > 1:
        env["LAUNCHER_SKIP_DEMO_RESET"] = "1"
    rc = _run(["/bin/bash", str(ROOT / "macos/launcher/agent_start.sh")], env=env, timeout=900)
    return rc == 0


def wait_trade_ready(*, port: int, timeout_sec: float, poll_sec: float) -> tuple[bool, list[PollSnapshot]]:
    deadline = time.monotonic() + timeout_sec
    polls: list[PollSnapshot] = []
    while time.monotonic() < deadline:
        snap = evaluate_trade_ready(port=port)
        polls.append(snap)
        _log(
            f"poll tier={snap.boot_tier} exec={snap.execution_loop_active} "
            f"armed={snap.routes_armed} feeds={snap.feeds_fresh} "
            f"gauge_sealed={snap.iron_gauge_sealed} accept={snap.accept} ({snap.accept_reason})"
        )
        if snap.accept:
            # Allow iron_gauge_tick to seal after execution plane is live
            for _ in range(6):
                time.sleep(2.0)
                snap2 = evaluate_trade_ready(port=port)
                if snap2.iron_gauge_sealed:
                    polls.append(snap2)
                    snap = snap2
                    break
            return True, polls
        time.sleep(poll_sec)
    return False, polls


def run_cycle(cycle: int, *, port: int, timeout_sec: float, poll_sec: float, gap_sec: float) -> CycleResult:
    started = time.monotonic()
    errors: list[str] = []
    result = CycleResult(
        cycle=cycle,
        started_at=_utc(),
        ended_at="",
        duration_sec=0.0,
        teardown_ok=False,
        start_ok=False,
        trade_ready=False,
    )

    pos = _open_positions()
    if pos > 0:
        errors.append(f"abort: {pos} open positions")
        result.errors = errors
        result.ended_at = _utc()
        result.duration_sec = round(time.monotonic() - started, 2)
        return result

    _log(f"=== CYCLE {cycle}: TEARDOWN ===")
    result.teardown_ok = teardown(port=port)
    if not result.teardown_ok:
        errors.append("teardown_failed")
        result.errors = errors
        result.ended_at = _utc()
        result.duration_sec = round(time.monotonic() - started, 2)
        return result

    if gap_sec > 0:
        _log(f"gap {gap_sec}s before start")
        time.sleep(gap_sec)

    _log(f"=== CYCLE {cycle}: START ===")
    result.start_ok = start_agent(port=port, cycle=cycle)
    if not result.start_ok:
        errors.append("agent_start_failed")

    ready, polls = wait_trade_ready(port=port, timeout_sec=timeout_sec, poll_sec=poll_sec)
    result.polls = [asdict(p) for p in polls]
    result.trade_ready = ready and result.start_ok
    if not result.trade_ready:
        errors.append("trade_ready_timeout")
    if polls:
        result.final = asdict(polls[-1])

    result.errors = errors
    result.ended_at = _utc()
    result.duration_sec = round(time.monotonic() - started, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Chaos restart proof harness")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--gap-sec", type=float, default=30.0)
    parser.add_argument("--timeout-sec", type=float, default=420.0)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log(
        f"chaos_restart_prove begin cycles={args.cycles} gap={args.gap_sec}s "
        f"timeout={args.timeout_sec}s port={args.port}"
    )

    report: dict[str, Any] = {
        "started_at": _utc(),
        "config": vars(args),
        "cycles": [],
        "passed": False,
    }

    all_ok = True
    for i in range(1, args.cycles + 1):
        cr = run_cycle(
            i,
            port=args.port,
            timeout_sec=args.timeout_sec,
            poll_sec=args.poll_sec,
            gap_sec=args.gap_sec if i > 1 else 0.0,
        )
        report["cycles"].append(asdict(cr))
        ok = cr.teardown_ok and cr.start_ok and cr.trade_ready
        _log(
            f"CYCLE {i} {'PASS' if ok else 'FAIL'} "
            f"duration={cr.duration_sec}s errors={cr.errors}"
        )
        all_ok = all_ok and ok
        if i < args.cycles and args.gap_sec > 0:
            _log(f"inter-cycle gap {args.gap_sec}s")
            time.sleep(args.gap_sec)

    report["ended_at"] = _utc()
    report["passed"] = all_ok
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _log(f"report written: {REPORT_PATH}")
    _log(f"RESULT: {'ALL CYCLES PASSED' if all_ok else 'FAILURES DETECTED'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
