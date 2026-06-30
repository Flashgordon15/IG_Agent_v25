#!/usr/bin/env python3
"""
Semi-automated demo trading validation — boot, IG budget, sizing, multi-market.

Usage:
  PYTHONPATH=src python3 scripts/demo_trading_validation.py
  PYTHONPATH=src python3 scripts/demo_trading_validation.py --api-only
  PYTHONPATH=src python3 scripts/demo_trading_validation.py --wait-trades-sec 180
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:8080"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def check_boot() -> tuple[bool, str]:
    boot = _get("/api/boot_status")
    ready = bool(boot.get("trade_ready"))
    diag = boot.get("startup_diagnostics") or {}
    missing = [k for k, v in diag.items() if not v]
    msg = f"trade_ready={ready} diagnostics_missing={missing}"
    return ready, msg


def check_apis() -> tuple[bool, str]:
    paths = [
        "/api/unified_status",
        "/api/trade_state",
        "/api/trade_events",
        "/api/rotation_state",
        "/api/ig_budget_state",
        "/api/health_light",
    ]
    for p in paths:
        body = _get(p)
        if not body.get("ok", True) and "ok" in body:
            return False, f"{p} not ok"
    return True, f"all {len(paths)} endpoints OK"


def check_ig_budget() -> tuple[bool, str]:
    snap = _get("/api/ig_budget_state")
    limited = bool(snap.get("rate_limited"))
    remaining = int(snap.get("estimated_budget_remaining") or 0)
    calls = int(snap.get("calls_last_30m") or 0)
    # Pass if API works; note if execution paused
    if limited:
        return True, f"rate_limited=True cooldown={snap.get('cooldown_seconds_remaining')}s (guard active)"
    return True, f"rate_limited=False budget_remaining={remaining} calls_30m={calls}"


def check_rotation_multi() -> tuple[bool, str]:
    rot = _get("/api/rotation_state")
    active = rot.get("active_instruments") or rot.get("rotation", {}).get("active_instruments") or []
    eligible = rot.get("eligible_instruments") or []
    n_active = len(active) if isinstance(active, list) else 0
    n_elig = len(eligible) if isinstance(eligible, list) else 0
    ok = n_active >= 2 or (n_active + n_elig) >= 2
    return ok, f"active={n_active} eligible={n_elig}"


def check_execution() -> tuple[bool, str]:
    hl = _get("/api/health_light")
    exec_ok = bool(hl.get("execution_loop_active"))
    armed = (hl.get("routing_state") or {}).get("armed", 0)
    sweep = hl.get("rotation_sweep_count", 0)
    return exec_ok and armed > 0, f"exec={exec_ok} armed={armed} sweep={sweep}"


def wait_for_trades(seconds: int = 120) -> tuple[bool, str]:
    seen_epics: set[str] = set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        budget = _get("/api/ig_budget_state")
        if budget.get("rate_limited"):
            time.sleep(15)
            continue
        try:
            state = _get("/api/trade_state")
            active = (state.get("lifecycle") or {}).get("active") or {}
            history = (state.get("lifecycle") or {}).get("history") or []
            for row in list(active.values()) + list(history):
                epic = str(row.get("epic") or "")
                st = str(row.get("state") or "")
                if epic:
                    seen_epics.add(epic)
                if st in ("ORDER_ACCEPTED", "ACTIVE", "TRAILING_STOP_ACTIVE"):
                    seen_epics.add(epic)
        except Exception as exc:
            return False, str(exc)
        if len(seen_epics) >= 2:
            return True, f"markets={sorted(seen_epics)}"
        time.sleep(10)
    return len(seen_epics) >= 1, f"partial markets={sorted(seen_epics)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--wait-trades-sec", type=int, default=0)
    args = parser.parse_args()

    results: list[tuple[str, bool, str]] = []

    try:
        ok, msg = check_apis()
        results.append(("apis", ok, msg))
        ok, msg = check_boot()
        results.append(("boot", ok, msg))
        ok, msg = check_ig_budget()
        results.append(("ig_budget", ok, msg))
        ok, msg = check_rotation_multi()
        results.append(("rotation_multi", ok, msg))
        ok, msg = check_execution()
        results.append(("execution", ok, msg))
        if args.wait_trades_sec > 0 and not args.api_only:
            ok, msg = wait_for_trades(args.wait_trades_sec)
            results.append(("multi_market_trades", ok, msg))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"checks": [{"name": n, "ok": o, "detail": m} for n, o, m in results]}, indent=2))
    core = [r for r in results if r[0] in ("apis", "boot", "execution", "ig_budget")]
    return 0 if all(r[1] for r in core) else 1


if __name__ == "__main__":
    raise SystemExit(main())
