#!/usr/bin/env python3
"""
Forensic trade-flow monitor — polls health/telemetry and tails hot logs.

Usage:
  PYTHONPATH=src python3 scripts/forensic_trade_flow_monitor.py [--duration 300]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_LOG = ROOT / "src/data/v31-production/logs/strategy_eval.log"
RESTART_LOG = ROOT / "src/data/logs/agent_restart.log"
BLOCKERS = ROOT / "blockers.log"


def _get(url: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"_error": str(exc)}


def _tail_lines(path: Path, n: int = 3) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 8192)
            fh.seek(max(0, size - chunk))
            text = fh.read().decode("utf-8", errors="replace")
        return [ln for ln in text.splitlines() if ln.strip()][-n:]
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Forensic IG Agent trade-flow monitor")
    parser.add_argument("--duration", type=int, default=300, help="Seconds to run (0=forever)")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval seconds")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    t0 = time.time()
    last_strategy_size = STRATEGY_LOG.stat().st_size if STRATEGY_LOG.is_file() else 0
    last_restart_size = RESTART_LOG.stat().st_size if RESTART_LOG.is_file() else 0
    pierce_matches = 0
    slow_streak: list[float] = []

    print(f"[forensic] monitoring {base} interval={args.interval}s duration={args.duration}s")
    sys.stdout.flush()

    while True:
        now = time.time()
        elapsed = now - t0
        if args.duration > 0 and elapsed >= args.duration:
            break

        t_poll = time.time()
        health = _get(f"{base}/api/health")
        telemetry = _get(f"{base}/api/v31/telemetry")
        poll_ms = (time.time() - t_poll) * 1000.0
        slow_streak.append(poll_ms)
        if len(slow_streak) > 5:
            slow_streak.pop(0)

        phase = (health.get("system_state") or {}).get("phase") or health.get("status")
        z = telemetry.get("live_calculated_zscore") or telemetry.get("volatility_z_score")
        suppress = telemetry.get("last_gate_suppression_reason") or ""
        ticks = telemetry.get("ticks_processed")
        positions = len(telemetry.get("active_positions") or [])

        if STRATEGY_LOG.is_file():
            sz = STRATEGY_LOG.stat().st_size
            if sz > last_strategy_size:
                for ln in _tail_lines(STRATEGY_LOG, 5):
                    if "Match: True" in ln:
                        pierce_matches += 1
                last_strategy_size = sz

        new_engine: list[str] = []
        if RESTART_LOG.is_file():
            sz = RESTART_LOG.stat().st_size
            if sz > last_restart_size:
                new_engine = [
                    ln
                    for ln in _tail_lines(RESTART_LOG, 8)
                    if any(
                        k in ln
                        for k in (
                            "DualCoreCoordinator",
                            "ParallelStrategySweep",
                            "MICRO-",
                            "place_market",
                            "IGRestGovernor",
                            "dispatch blocked",
                            "lazy attach",
                        )
                    )
                ]
                last_restart_size = sz

        warn = ""
        if max(slow_streak) > 10_000:
            warn = " SLOW_POLL>10s"

        print(
            f"[{elapsed:6.0f}s] phase={phase} z={z} suppress={suppress!r} "
            f"ticks={ticks} pos={positions} pierce_hits~={pierce_matches}{warn}"
        )
        for ln in new_engine:
            print(f"  ENGINE> {ln[-200:]}")
        sys.stdout.flush()

        time.sleep(max(0.5, args.interval))

    print(f"[forensic] done elapsed={time.time()-t0:.0f}s pierce_match_lines~={pierce_matches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
