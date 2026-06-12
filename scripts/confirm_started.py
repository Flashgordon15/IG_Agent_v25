#!/usr/bin/env python3
"""Verify IG Agent v29 finished startup and is trading-ready."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="IG Agent v29 — confirm startup succeeded"
    )
    parser.add_argument(
        "--within-minutes",
        type=float,
        default=10.0,
        help="How far back to search engine.log for stream_ready (default: 10)",
    )
    parser.add_argument(
        "--max-gate-age",
        type=float,
        default=120.0,
        help="Maximum acceptable gate-check age in seconds (default: 120)",
    )
    parser.add_argument(
        "--allow-closed-markets",
        action="store_true",
        help="Pass when loops run but quotes stale (weekends / closed sessions)",
    )
    args = parser.parse_args()

    from system.shutdown_cleanup import (
        _fetch_api_health,
        _instance_lock_holder_pid,
        _list_main_py_pids,
        _port_bound,
        agent_fully_started,
    )

    ok, issues = agent_fully_started(
        max_gate_age_sec=args.max_gate_age,
        stream_log_within_min=args.within_minutes,
        require_trading_healthy=not args.allow_closed_markets,
    )

    pids = _list_main_py_pids()
    health = _fetch_api_health()
    lock_pid = _instance_lock_holder_pid()

    try:
        from api.agent_health import _watchdog_active

        watchdog_ok = _watchdog_active()
    except Exception:
        watchdog_ok = False

    gate_age = health.get("last_gate_check_age_sec") if health else None
    gate_ok = gate_age is not None and float(gate_age) <= float(args.max_gate_age)
    stream_ok = "stream_ready not in recent engine.log" not in issues

    print()
    from system.app_identity import APP_DISPLAY_NAME, APP_VERSION_LABEL

    print(f"{APP_DISPLAY_NAME} {APP_VERSION_LABEL} — CONFIRM STARTED")
    print("=" * 40)
    checks = [
        (
            "Single main.py process",
            bool(pids)
            and len(pids) == 1
            and not any("duplicate main.py" in i for i in issues),
        ),
        ("Port 8080 bound", _port_bound()),
        (
            "Instance lock held",
            lock_pid is not None and not any("instance lock" in i for i in issues),
        ),
        ("Watchdog running", watchdog_ok),
        (
            "Health API responding",
            health is not None and "cannot reach /api/health" not in issues,
        ),
        (
            "Trading loops running",
            bool(health and health.get("trading_loops_running")),
        ),
        (
            "Trading healthy",
            bool(health and health.get("trading_healthy"))
            if not args.allow_closed_markets
            else bool(health and health.get("trading_loops_running")),
        ),
        ("Gate check recent", gate_ok),
        ("stream_ready in engine.log", stream_ok),
    ]
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    print("=" * 40)
    if ok:
        print("→ STARTUP OK — agent is trading-ready")
        return 0
    print("→ STARTUP INCOMPLETE — remaining issues:")
    for issue in issues:
        print(f"  - {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
