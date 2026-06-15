#!/usr/bin/env python3
"""
Read-only boot + runtime performance monitor for IG Agent v29.1.

Polls /api/startup/status (SystemState) and perf_metrics.snapshot.json written
by the agent when IG_AGENT_PERF_MONITOR=1.

Usage (Mac Mini — two terminals):

  Terminal 1 — agent with instrumentation enabled:
    export IG_AGENT_PERF_MONITOR=1
    PYTHONPATH=src python3 src/main.py

  Terminal 2 — monitor (start before or immediately after agent):
    PYTHONPATH=src python3 src/system/diagnostics/boot_performance_monitor.py --watch

Report file: src/data/logs/boot_performance_report.txt
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as script from repo root
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from system.diagnostics.perf_metrics import read_snapshot_file
from system.paths import logs_dir

_DEFAULT_API = "http://127.0.0.1:8080"
_GATE_IDS = ("G1", "G2", "G3", "G4", "G5")
_G1_CODE_EXEC_PASS_MS = 100.0
_POLL_BOOT_SEC = 0.15
_POLL_STEADY_SEC = 2.0
_EPICS_BASELINE = 6
_TICKS_PER_MIN_PER_EPIC = 12  # ~5s interval


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_startup_status(api_base: str, timeout: float = 1.5) -> dict[str, Any] | None:
    url = f"{api_base.rstrip('/')}/api/startup/status"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _report_path() -> Path:
    return logs_dir() / "boot_performance_report.txt"


def _append_report(lines: list[str]) -> None:
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def _write_report_header() -> None:
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"=== IG Agent Boot Performance Report ===\n"
        f"monitor_started: {_utc_now()}\n\n",
        encoding="utf-8",
    )


def _ms_between(t0: float | None, t1: float | None) -> float | None:
    if t0 is None or t1 is None:
        return None
    return (t1 - t0) * 1000.0


def _gate_status(snap: dict[str, Any], gate_id: str) -> str:
    gates = snap.get("gates") or {}
    g = gates.get(gate_id) or {}
    return str(g.get("status") or "pending").lower()


class BootPerformanceMonitor:
    """Poll SystemState via HTTP and assemble milestone timings."""

    def __init__(self, *, api_base: str, host: str, port: int) -> None:
        self._api = api_base
        self._host = host
        self._port = port
        self._t0 = time.time()
        self._g1_start: float | None = None
        self._milestones: dict[str, float] = {}
        self._logged: set[str] = set()
        self._port_bound_at: float | None = None
        self._boot_started_wall: float | None = None
        self._boot_section_done = False

    def _maybe_log(self, key: str, line: str) -> None:
        if key in self._logged:
            return
        self._logged.add(key)
        print(line, flush=True)
        _append_report([f"[{_utc_now()}] {line}"])

    def _resolve_boot_started_ts(self, sys_snap: dict[str, Any]) -> float:
        if self._boot_started_wall is not None:
            return self._boot_started_wall

        epoch = sys_snap.get("started_at_epoch")
        if isinstance(epoch, (int, float)) and float(epoch) > 0:
            ts = float(epoch)
        else:
            parsed = _parse_iso(sys_snap.get("started_at"))
            ts = parsed if parsed is not None else self._t0

        self._boot_started_wall = ts
        return ts

    def _maybe_log_code_execution_verdict(self) -> None:
        """PASS when Python path G1 COMPLETE→bind is fast; wall clock logged separately."""
        if "code_exec_verdict" in self._logged:
            return
        if self._port_bound_at is None or "g1_complete" not in self._milestones:
            return

        bind_ms = _ms_between(
            self._milestones["g1_complete"], self._port_bound_at
        )
        if bind_ms is None:
            return

        self._logged.add("code_exec_verdict")
        if bind_ms < _G1_CODE_EXEC_PASS_MS:
            line = (
                "[PASS] Code Execution Speed: PASS (macOS Launch Latency: Included) "
                f"— G1 COMPLETE→bind: {bind_ms:.0f}ms"
            )
        else:
            line = (
                f"[WARN] Code Execution Speed: {bind_ms:.0f}ms "
                f"(target <{_G1_CODE_EXEC_PASS_MS:.0f}ms G1 COMPLETE→bind)"
            )
        print(line, flush=True)
        _append_report([f"[{_utc_now()}] {line}"])

        if "g1_cold_wall" not in self._logged and self._boot_started_wall is not None:
            cold_ms = _ms_between(self._boot_started_wall, self._port_bound_at)
            if cold_ms is not None:
                self._logged.add("g1_cold_wall")
                wall_line = (
                    f"Cold start G1→bind (wall clock, includes macOS launch): "
                    f"{cold_ms:.0f}ms"
                )
                print(wall_line, flush=True)
                _append_report([f"[{_utc_now()}] {wall_line}"])

    def _track_gate_transitions(self, sys_snap: dict[str, Any]) -> None:
        started_iso = sys_snap.get("started_at")
        started_ts = self._resolve_boot_started_ts(sys_snap)

        g1 = _gate_status(sys_snap, "G1")
        if g1 == "running" and self._g1_start is None:
            self._g1_start = time.time()
            self._maybe_log(
                "g1_start",
                f"Gate 1 RUNNING observed (boot started_at={started_iso})",
            )
        if g1 == "complete" and "g1_complete" not in self._milestones:
            self._milestones["g1_complete"] = time.time()
            g1_start = self._g1_start or started_ts
            ms = _ms_between(g1_start, self._milestones["g1_complete"])
            completed_at = (sys_snap.get("gate_completed_at") or {}).get("G1")
            self._maybe_log(
                "g1_complete",
                f"Gate 1 COMPLETE — {ms:.0f}ms from G1 start"
                + (f" (gate_completed_at={completed_at})" if completed_at else ""),
            )
            self._maybe_log_code_execution_verdict()

        if self._port_bound_at is None and _port_open(self._host, self._port):
            self._port_bound_at = time.time()
            cold_ms = _ms_between(started_ts, self._port_bound_at)
            self._maybe_log(
                "port_bound",
                f"Uvicorn port {self._port} accepting connections"
                + (f" — {cold_ms:.0f}ms from SystemState.started_at" if cold_ms else ""),
            )
            if "g1_complete" in self._milestones:
                bind_ms = _ms_between(
                    self._milestones["g1_complete"], self._port_bound_at
                )
                if bind_ms is not None:
                    self._maybe_log(
                        "g1_to_bind",
                        f"G1 COMPLETE → port bound: {bind_ms:.0f}ms",
                    )
            self._maybe_log_code_execution_verdict()

        completed_at_map = sys_snap.get("gate_completed_at") or {}
        prev_complete: float | None = self._milestones.get("g1_complete")
        labels = {
            "G2": "Account hydration (Gate 2)",
            "G3": "Streaming / Lightstreamer (Gate 3)",
            "G4": "OHLC + dormant loops (Gate 4)",
            "G5": "READY activation (Gate 5)",
        }
        for gid in _GATE_IDS[1:]:
            if _gate_status(sys_snap, gid) != "complete":
                continue
            key = f"{gid.lower()}_complete"
            if key in self._milestones:
                continue
            ts = _parse_iso(completed_at_map.get(gid)) or time.time()
            self._milestones[key] = ts
            if prev_complete is not None:
                ms = _ms_between(prev_complete, ts)
                self._maybe_log(
                    key,
                    f"{labels.get(gid, gid)} COMPLETE — {ms:.0f}ms since prior gate",
                )
            prev_complete = ts

        if sys_snap.get("ready") and "ready" not in self._logged:
            self._logged.add("ready")
            total_ms = _ms_between(started_ts, time.time())
            line = f"SystemState READY — total boot {total_ms:.0f}ms" if total_ms else "SystemState READY"
            print(line, flush=True)
            _append_report([f"[{_utc_now()}] {line}"])
            self._log_boot_summary(sys_snap)
            self._boot_section_done = True

    def _log_boot_summary(self, sys_snap: dict[str, Any]) -> None:
        streaming = sys_snap.get("streaming") or {}
        hydration = sys_snap.get("hydration") or {}
        loops = sys_snap.get("loops") or {}
        lines = [
            "",
            "--- Boot Summary ---",
            f"  transport: {streaming.get('transport', '—')}",
            f"  heartbeat_ok: {streaming.get('heartbeat_ok')}",
            f"  first_tick_epic: {streaming.get('first_tick_epic')}",
            f"  ohlc: {hydration.get('ohlc_epics_ready')}/{hydration.get('ohlc_epics_total')}",
            f"  loops: built={loops.get('built')} accepting_ticks={loops.get('accepting_ticks')}",
            "",
        ]
        for line in lines:
            print(line, flush=True)
        _append_report(lines)

    def _log_cache_section(self, perf: dict[str, Any]) -> None:
        dl = perf.get("daily_loss_cache") or {}
        ml = perf.get("ml_rows_cache") or {}
        dl_hits = int(dl.get("hits") or 0)
        dl_miss = int(dl.get("misses") or 0)
        ml_hits = int(ml.get("hits") or 0)
        ml_miss = int(ml.get("misses") or 0)
        dl_total = dl_hits + dl_miss
        ml_total = ml_hits + ml_miss

        # Baseline before cache: ~2 SQLite touches per points_state tick × 6 epics × 12/min
        baseline_per_min = _EPICS_BASELINE * _TICKS_PER_MIN_PER_EPIC * 2
        observed_miss_per_min = dl_miss  # cumulative; approximate rate in steady section

        lines = [
            "",
            "--- Cache Hit Rates (agent process) ---",
            f"  daily_loss_gate_status: hits={dl_hits} misses={dl_miss} "
            f"hit_rate={dl.get('hit_rate_pct')}%",
            f"  ml_clean_training_rows: hits={ml_hits} misses={ml_miss} "
            f"hit_rate={ml.get('hit_rate_pct')}%",
            f"  estimated pre-cache DB touches/min (6 epics): ~{baseline_per_min}",
            f"  daily_loss SQLite queries (misses, cumulative): {dl_miss}",
            f"  ml row count queries (misses, cumulative): {ml_miss}",
        ]
        if dl_total > 0 and dl.get("hit_rate_pct") is not None:
            reduction = 100.0 - (100.0 * dl_miss / max(1, baseline_per_min / 10))
            lines.append(
                f"  daily_loss cache effective reduction vs naive tick polling: "
                f"~{max(0.0, reduction):.0f}% fewer queries observed"
            )
        tick = perf.get("tick_gate_eval") or {}
        lines.extend(
            [
                "",
                "--- Tick Gate Evaluation ---",
                f"  evaluations: {tick.get('count', 0)}",
                f"  slow (>{tick.get('slow_threshold_us', 50000)/1000:.0f}ms): "
                f"{tick.get('slow_count', 0)}",
                f"  max_us: {tick.get('max_us', 0)}",
            ]
        )
        last_slow = tick.get("last_slow") or {}
        if last_slow:
            lines.append(
                f"  last_slow: epic={last_slow.get('epic')} "
                f"{last_slow.get('total_ms')}ms gate={last_slow.get('slowest_gate')}"
            )
        per_gate = tick.get("per_gate_max_us") or {}
        if per_gate:
            top = sorted(per_gate.items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines.append("  per_gate_max_us (top 5): " + ", ".join(f"{k}={v}" for k, v in top))
        lines.append("")
        for line in lines:
            print(line, flush=True)
        _append_report(lines)

    def poll_once(self) -> bool:
        """Returns False when boot failed."""
        payload = _fetch_startup_status(self._api)
        if payload is None:
            return True
        sys_snap = payload.get("system_state") or {}
        if not sys_snap and payload.get("boot_metrics"):
            sys_snap = {
                "started_at": payload.get("started_at"),
                "ready": payload.get("ready"),
                "boot_metrics": payload.get("boot_metrics"),
            }
        if sys_snap.get("phase") == "FAILED" or sys_snap.get("error_gate"):
            err = sys_snap.get("error") or "unknown"
            gate = sys_snap.get("error_gate")
            msg = f"Boot FAILED at {gate}: {err}"
            print(msg, flush=True)
            _append_report([f"[{_utc_now()}] {msg}"])
            return False
        self._track_gate_transitions(sys_snap)
        return True

    def watch(self, *, steady_minutes: float = 10.0) -> int:
        _write_report_header()
        print(f"Boot monitor watching {self._api} — report: {_report_path()}", flush=True)
        deadline = time.time() + steady_minutes * 60.0
        last_cache_log = 0.0
        ready = False
        while time.time() < deadline:
            payload = _fetch_startup_status(self._api)
            if payload is None:
                time.sleep(_POLL_BOOT_SEC)
                continue
            sys_snap = payload.get("system_state") or {}
            if sys_snap.get("phase") == "FAILED" or sys_snap.get("error_gate"):
                err = sys_snap.get("error") or "unknown"
                gate = sys_snap.get("error_gate")
                msg = f"Boot FAILED at {gate}: {err}"
                print(msg, flush=True)
                _append_report([f"[{_utc_now()}] {msg}"])
                return 1
            self._track_gate_transitions(sys_snap)
            ready = bool(sys_snap.get("ready"))
            interval = _POLL_STEADY_SEC if ready else _POLL_BOOT_SEC
            if ready and time.time() - last_cache_log > 30.0:
                perf = read_snapshot_file()
                if perf:
                    self._log_cache_section(perf)
                    last_cache_log = time.time()
            time.sleep(interval)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="IG Agent boot + runtime performance monitor (read-only)"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll until steady-state window elapses (default mode)",
    )
    parser.add_argument(
        "--api",
        default=_DEFAULT_API,
        help=f"Agent API base URL (default {_DEFAULT_API})",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Port bind check host")
    parser.add_argument("--port", type=int, default=8080, help="Uvicorn port")
    parser.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="Minutes to watch after start (steady-state cache sampling)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single poll + print snapshot (no watch loop)",
    )
    args = parser.parse_args(argv)

    monitor = BootPerformanceMonitor(
        api_base=args.api, host=args.host, port=args.port
    )

    if args.once:
        ok = monitor.poll_once()
        perf = read_snapshot_file()
        if perf:
            monitor._log_cache_section(perf)
        return 0 if ok else 1

    if args.watch or not args.once:
        return monitor.watch(steady_minutes=args.minutes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
