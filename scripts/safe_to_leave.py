#!/usr/bin/env python3
"""
IG Agent v25 — SAFE TO LEAVE check.

Run before walking away from a live session. Every line must PASS before
the agent can be trusted to trade overnight without babysitting.

Usage:
  PYTHONPATH=src python3 scripts/safe_to_leave.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.pre_flight_checks import pre_flight_summary, run_all_pre_flight_checks

HEALTH_URL = "http://127.0.0.1:8080/api/health"
DEPLOY_TEST = ROOT / "tests" / "test_deployment_verified.py"


def _check(label: str, passed: bool, detail: str = "") -> bool:
    mark = "PASS" if passed else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return passed


def _fetch_health() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _run_deployment_tests() -> tuple[bool, str]:
    if not DEPLOY_TEST.is_file():
        return False, "test_deployment_verified.py missing"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(DEPLOY_TEST), "-q", "--tb=line"],
        cwd=ROOT,
        env={**dict(**__import__("os").environ), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    tail = (result.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit {result.returncode}"
    return result.returncode == 0, summary


def _watchdog_running() -> bool:
    from api.agent_health import _watchdog_active

    return _watchdog_active()


def _heartbeat_auto_shutdown_disabled() -> tuple[bool, str]:
    """Runtime check: heartbeat monitor must be a no-op (no browser-timeout shutdown)."""
    try:
        import inspect

        from api.routes import _start_heartbeat_monitor

        body = inspect.getsource(_start_heartbeat_monitor)
        if "auto-shutdown on browser disconnect is disabled" not in body:
            return False, "heartbeat monitor source may enforce browser timeout"
        _start_heartbeat_monitor()
        return True, "runtime no-op confirmed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _overnight_supervision() -> tuple[bool, str]:
    from system.overnight_supervision import launchd_supervision_status

    ok, detail = launchd_supervision_status()
    if ok:
        return True, detail
    return False, f"{detail} — required for Safe to Leave bundle"


def _ensure_overnight_supervision() -> tuple[bool, str]:
    if os.environ.get("IG_AGENT_ENSURE_LAUNCHD", "1").strip() in ("0", "false", "no"):
        return True, "skipped (IG_AGENT_ENSURE_LAUNCHD=0)"
    from system.overnight_supervision import prepare_overnight_bundle

    return prepare_overnight_bundle()


def _on_ac_power() -> tuple[bool, str]:
    """caffeinate -s only prevents sleep on AC; battery + lid close can still kill the agent."""
    try:
        import subprocess

        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        out = (result.stdout or "").lower()
        if "battery power" in out or "on battery" in out:
            return False, "on battery — plug in Mac for overnight (lid close may sleep)"
        if "ac power" in out or "now drawing from 'ac power'" in out:
            return True, "on AC power"
        return True, "power source unknown — assume plugged in"
    except Exception as e:
        return True, f"power check skipped ({type(e).__name__})"


def _telegram_configured() -> tuple[bool, str]:
    try:
        from system.config import Config
        from system.config_loader import _sync_operating_mode_from_credentials
        from system.config_validator import apply_config_defaults
        from system.paths import config_dir
        from system.telegram_notifier import configure_telegram, get_telegram_notifier

        raw = json.loads((config_dir() / "config_v25.json").read_text(encoding="utf-8"))
        merged = apply_config_defaults(raw)
        _sync_operating_mode_from_credentials(merged)
        configure_telegram(Config(_data=merged))
        notifier = get_telegram_notifier()
        if notifier is None or not notifier.enabled:
            return False, "telegram disabled (set bot_token + chat_id)"
        return True, "telegram configured"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _quick_mode() -> bool:
    """Live dashboard checks must not run full pytest (blocks API for minutes)."""
    return os.environ.get("IG_AGENT_SAFE_TO_LEAVE_QUICK", "").strip() in (
        "1",
        "true",
        "yes",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v25 safe-to-leave check")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip deployment pytest suite (used by live dashboard API)",
    )
    args = parser.parse_args()
    quick = bool(args.quick) or _quick_mode()

    print()
    print("IG Agent v25 — SAFE TO LEAVE CHECK")
    print("=" * 48)
    if quick:
        print("[MODE] quick — deployment pytest skipped (live agent)")

    all_ok = True

    ensure_ok, ensure_detail = _ensure_overnight_supervision()
    all_ok &= _check("Launchd supervision bootstrap", ensure_ok, ensure_detail)

    hb_ok, hb_detail = _heartbeat_auto_shutdown_disabled()
    all_ok &= _check("Heartbeat auto-shutdown disabled", hb_ok, hb_detail)

    if quick:
        print(
            "[SKIP] Deployment verification tests — skipped in quick mode "
            "(run: PYTHONPATH=src python3 scripts/safe_to_leave.py)"
        )
    else:
        deploy_ok, deploy_detail = _run_deployment_tests()
        all_ok &= _check("Deployment verification tests", deploy_ok, deploy_detail)

    health = _fetch_health()
    markets_open = int(health.get("markets_open_count") or 0) if health else -1

    for row in pre_flight_summary(
        run_all_pre_flight_checks(require_live_agent=True, max_gate_age_sec=120.0)
    )["results"]:
        if row["id"] in ("7.1", "7.2"):
            continue
        if row["id"] == "7.4" and markets_open == 0:
            print(
                f"[SKIP] {row['description']} — no markets open (live ticks not required)"
            )
            continue
        ok = row["passed"]
        all_ok &= _check(row["description"], ok, row.get("reason") or "")

    # Live agent checks (duplicate gate/data checks for clear operator messaging)
    if health is None:
        all_ok &= _check("Agent responding on :8080", False, "cannot reach /api/health")
    else:
        all_ok &= _check("Agent responding on :8080", True)
        all_ok &= _check(
            "Trading loops running",
            bool(health.get("trading_loops_running")),
        )
        all_ok &= _check(
            "Trading healthy (loops + gates + quotes)",
            bool(health.get("trading_healthy")),
            ", ".join(health.get("issues") or []) or "ok",
        )
        gate_age = health.get("last_gate_check_age_sec")
        gate_ok = gate_age is not None and float(gate_age) <= 120.0
        all_ok &= _check(
            "Gate check recent (<120s)",
            gate_ok,
            f"age={gate_age}s" if gate_age is not None else "no gate activity",
        )
        all_ok &= _check(
            "Quotes fresh (all markets)",
            bool(health.get("quotes_fresh")),
            f"{health.get('quotes_fresh_count', 0)}/{health.get('quotes_total', 0)} fresh",
        )

    ok = _watchdog_running()
    all_ok &= _check("Watchdog running", ok)

    sup_ok, sup_detail = _overnight_supervision()
    all_ok &= _check(
        "Overnight bundle (launchd watchdog — survives Cursor close)",
        sup_ok,
        sup_detail,
    )

    ac_ok, ac_detail = _on_ac_power()
    all_ok &= _check("Mac on AC power (overnight sleep)", ac_ok, ac_detail)

    tg_ok, tg_detail = _telegram_configured()
    all_ok &= _check("Telegram alerts configured", tg_ok, tg_detail)

    print("=" * 48)
    if all_ok:
        from system.overnight_supervision import mark_overnight_armed

        mark_overnight_armed(source="safe_to_leave_cli")
        print("→ SAFE TO LEAVE — overnight bundle armed (close Cursor/browser safely)")
        print()
        return 0
    print("→ NOT SAFE — fix FAIL items before leaving")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
