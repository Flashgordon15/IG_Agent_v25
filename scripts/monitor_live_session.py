#!/usr/bin/env python3
"""60-minute live session monitor — trades, signals, blocks, health."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8080"
DURATION_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
INTERVAL_SEC = 60
OUT = Path("/tmp/ig_session_monitor_report.json")
LOG_CANDIDATES = [
    Path("/tmp/ig_agent.foreground.log"),
    Path("/tmp/ig_agent.live_boot.log"),
    ROOT / "src/data/logs/production.log",
    Path.home()
    / "Library/Application Support/IG Agent Apex/v30-production/data/logs/ig_agent.log",
    Path.home()
    / "Library/Application Support/IG Agent Apex/v30-production/data/logs/production.log",
]
SHADOW_LOG = ROOT / "src/data/shadow_log.jsonl"

LOG_PATTERNS = {
    "exec_blocked": re.compile(r"EXEC BLOCKED", re.I),
    "exec_ok": re.compile(r"EXEC OK", re.I),
    "fitness_fail": re.compile(r"environment_fitness gate: score failed", re.I),
    "fitness_pass": re.compile(r"environment_fitness.*passed|fitness gate pass", re.I),
    "order_dispatch": re.compile(r"LiveExecutor dispatched|order.*dispatch", re.I),
    "points_error": re.compile(r"_points|AttributeError.*_points", re.I),
    "kernel_fail": re.compile(r"KernelInterceptor HARD FAIL", re.I),
    "tick_error": re.compile(r"trading_loop tick error", re.I),
    "spread_block": re.compile(r"spread.*block|entry_blocked", re.I),
    "signal_wait": re.compile(r"signal=WAIT|direction=WAIT", re.I),
}


def _get(url: str, timeout: float = 5.0) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _resolve_log() -> Path | None:
    for p in LOG_CANDIDATES:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _count_shadow_lines_since(offset: int) -> tuple[int, int]:
    if not SHADOW_LOG.exists():
        return 0, 0
    fired = 0
    total = 0
    try:
        with SHADOW_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                total += 1
                try:
                    row = json.loads(line)
                    if row.get("would_have_fired"):
                        fired += 1
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return total, fired


def _scan_log(path: Path, offset: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {k: [] for k in LOG_PATTERNS}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                for key, pat in LOG_PATTERNS.items():
                    if pat.search(line):
                        counts[key] += 1
                        if len(samples[key]) < 5:
                            samples[key].append(line.strip()[:240])
    except OSError:
        pass
    return {"counts": dict(counts), "samples": {k: v for k, v in samples.items() if v}}


def _agent_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def _snapshot(trades: dict | None, shadow: dict | None, health: dict | None) -> dict:
    active = (trades or {}).get("active") or []
    closed = (trades or {}).get("closed") or []
    bm = (health or {}).get("boot_metrics") or {}
    ss = bm.get("system_state") or {}
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_pid": (health or {}).get("agent_pid"),
        "ready": bm.get("ready"),
        "trading_healthy": (health or {}).get("trading_healthy"),
        "loops": ss.get("loops"),
        "issues": (health or {}).get("issues") or [],
        "active_trades": len(active),
        "closed_trades": len(closed),
        "shadow_evaluations": (shadow or {}).get("evaluations"),
        "shadow_would_trade": (shadow or {}).get("would_have_traded"),
        "top_blocked_setup": (shadow or {}).get("top_blocked_setup"),
    }


def main() -> int:
    started = time.time()
    log_path = _resolve_log()
    log_offset = log_path.stat().st_size if log_path else 0
    shadow_offset = SHADOW_LOG.stat().st_size if SHADOW_LOG.exists() else 0

    baseline_health = _get(f"{API}/api/health")
    baseline_trades = _get(f"{API}/api/trades")
    baseline_shadow = _get(f"{API}/api/shadow/today")
    baseline_fulfillment = _get(f"{API}/api/unified/fulfillment")
    start_pid = (baseline_health or {}).get("agent_pid")

    snapshots: list[dict] = []
    log_totals: Counter[str] = Counter()
    log_samples: dict[str, list[str]] = {}
    agent_died_at: str | None = None

    print(
        f"MONITOR_START pid={start_pid} duration={DURATION_SEC}s interval={INTERVAL_SEC}s",
        flush=True,
    )

    while time.time() - started < DURATION_SEC:
        if not _agent_alive(start_pid):
            agent_died_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"MONITOR_ALERT agent died pid={start_pid}", flush=True)
            break

        health = _get(f"{API}/api/health")
        trades = _get(f"{API}/api/trades")
        shadow = _get(f"{API}/api/shadow/today")
        fulfillment = _get(f"{API}/api/unified/fulfillment")
        signals = _get(f"{API}/api/signals?limit=20")

        snap = _snapshot(trades, shadow, health)
        snap["fulfillment_all_ready"] = (fulfillment or {}).get("all_ready")
        snap["signal_count"] = len((signals or {}).get("signals") or [])
        snapshots.append(snap)

        if log_path:
            scan = _scan_log(log_path, log_offset)
            for k, v in scan["counts"].items():
                log_totals[k] += v
            for k, lines in scan["samples"].items():
                log_samples.setdefault(k, []).extend(lines)
                log_samples[k] = log_samples[k][-8:]
            log_offset = log_path.stat().st_size

        elapsed = int(time.time() - started)
        print(
            f"MONITOR_TICK t={elapsed}s active={snap['active_trades']} "
            f"shadow_eval={snap['shadow_evaluations']} would={snap['shadow_would_trade']} "
            f"ready={snap['ready']} ticks={((snap.get('loops') or {}).get('accepting_ticks'))}",
            flush=True,
        )
        time.sleep(INTERVAL_SEC)

    end_health = _get(f"{API}/api/health")
    end_trades = _get(f"{API}/api/trades")
    end_shadow = _get(f"{API}/api/shadow/today")
    shadow_new_lines, shadow_new_fired = _count_shadow_lines_since(shadow_offset)

    b_active = len((baseline_trades or {}).get("active") or [])
    b_closed = len((baseline_trades or {}).get("closed") or [])
    e_active = len((end_trades or {}).get("active") or [])
    e_closed = len((end_trades or {}).get("closed") or [])

    report = {
        "monitor_started": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
        "monitor_ended": datetime.now(timezone.utc).isoformat(),
        "duration_sec": int(time.time() - started),
        "agent_pid_start": start_pid,
        "agent_alive_end": _agent_alive(start_pid),
        "agent_died_at": agent_died_at,
        "log_path": str(log_path) if log_path else None,
        "baseline": _snapshot(baseline_trades, baseline_shadow, baseline_health),
        "final": _snapshot(end_trades, end_shadow, end_health),
        "delta": {
            "active_trades": e_active - b_active,
            "closed_trades": e_closed - b_closed,
            "shadow_evaluations": (
                ((end_shadow or {}).get("evaluations") or 0)
                - ((baseline_shadow or {}).get("evaluations") or 0)
            ),
            "shadow_would_trade": (
                ((end_shadow or {}).get("would_have_traded") or 0)
                - ((baseline_shadow or {}).get("would_have_traded") or 0)
            ),
        },
        "trades_opened": [
            t
            for t in (end_trades or {}).get("closed") or []
            if t not in ((baseline_trades or {}).get("closed") or [])
        ],
        "trades_still_active": end_trades.get("active") if end_trades else [],
        "log_events": dict(log_totals),
        "log_samples": log_samples,
        "shadow_log_new_lines": shadow_new_lines,
        "shadow_log_new_fired": shadow_new_fired,
        "snapshots": snapshots,
        "fulfillment_baseline_all_ready": (baseline_fulfillment or {}).get("all_ready"),
    }
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"MONITOR_COMPLETE report={OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
