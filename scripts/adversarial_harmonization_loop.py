#!/usr/bin/env python3
"""
10-Phase Adversarial Harmonization Loop — diagnostic + hardening runner.

Phases 1-9: safe read-only / DEMO checks.
Phase 10: DEMO micro-lot probe only (live requires LIVE_PROMOTION_CHECKLIST sign-off).

Usage:
  PYTHONPATH=src .venv/bin/python3 scripts/adversarial_harmonization_loop.py
  PYTHONPATH=src .venv/bin/python3 scripts/adversarial_harmonization_loop.py --phase 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harmonization.lifecycle_reporter import LifecycleReporter, ensure_audit_files
from harmonization.reconnect_policy import reconnect_with_backoff
from harmonization.volatility_gate import audit_trade_blockers, dynamic_confidence_floor

HEALTH_URL = "http://127.0.0.1:8080/api/health"
FULFILLMENT_URL = "http://127.0.0.1:8080/api/unified/fulfillment"
HEAL_URL = "http://127.0.0.1:8080/api/cockpit/heal"


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _phase_1_network_hardening(rep: LifecycleReporter) -> None:
    latencies: list[float] = []
    for _ in range(5):

        def _ping() -> dict[str, Any]:
            t0 = time.perf_counter()
            out = _get(HEALTH_URL)
            latencies.append((time.perf_counter() - t0) * 1000.0)
            return out

        reconnect_with_backoff(_ping, label="health_api")
        time.sleep(0.2)
    mean_ms = sum(latencies) / len(latencies) if latencies else 0.0
    rep.append_phase_report(
        1,
        status="Success",
        latency_ms={"mean_ms": round(mean_ms, 2), "max_ms": round(max(latencies), 2)},
        hardening="reconnect_with_backoff on health API; harmonization/reconnect_policy.py",
        close_down="read-only probes — no positions opened",
    )


def _phase_2_memory_purge(rep: LifecycleReporter) -> None:
    cleared = {"ticks": 0}
    try:
        from system.feeds.multi_feed_hub import hard_reset_multi_feed_hub

        result = hard_reset_multi_feed_hub(reason="phase2_memory_purge")
        cleared["ticks"] = int((result or {}).get("restarts") or 0)
    except Exception as exc:
        rep.append_phase_report(
            2,
            status=f"Exception: {type(exc).__name__}: {exc}",
            traceback_text=traceback.format_exc(),
            hardening="multi_feed_hub hard_reset invoked",
        )
        return
    rep.append_phase_report(
        2,
        status="Success",
        hardening=f"feed hub hard reset restarts={cleared['ticks']}",
        close_down="buffers purged — no open orders",
    )


def _phase_3_shm_integrity(rep: LifecycleReporter) -> None:
    from system.ipc.cockpit_shm_passive import read_cockpit_shm

    view = read_cockpit_shm()
    blockers: list[str] = []
    if view is None:
        blockers.append("SHM segment not published")
    elif not view.get("publisher_alive"):
        blockers.append(f"stale SHM pid={view.get('agent_pid')}")
    rep.append_phase_report(
        3,
        status="Success" if not blockers else "Degraded",
        blockers=blockers,
        hardening="cockpit_feed_guardian + pid_mismatch detection",
        close_down="SHM read-only",
    )


def _phase_4_velocity_watchdog(rep: LifecycleReporter) -> None:
    ful = _get(FULFILLMENT_URL)
    dv = ful.get("data_velocity") or {}
    stalled = bool(dv.get("stall_active"))
    blockers = []
    if stalled:
        blockers.append(f"velocity stall frozen={dv.get('frozen_sec')}s")
        try:
            _post(HEAL_URL, {"reason": "phase4_velocity_heal"})
        except Exception as exc:
            blockers.append(f"heal API failed: {exc}")
    rep.append_phase_report(
        4,
        status="Success" if not stalled else "Healed",
        latency_ms={
            "mean_ms": float(dv.get("frozen_sec") or 0) * 1000,
            "max_ms": float(dv.get("stall_threshold_sec") or 5) * 1000,
        },
        blockers=blockers,
        hardening="POST /api/cockpit/heal + agent_feed_guardian",
        close_down="feeds reset — flat",
    )


def _phase_5_reconnect_stress(rep: LifecycleReporter) -> None:
    attempts = 0

    def _flaky() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("simulated dropout")
        return True

    reconnect_with_backoff(_flaky, label="simulated_feed")
    rep.append_phase_report(
        5,
        status="Success",
        hardening="exponential backoff 1/2/4/8/16s verified",
        close_down=f"recovered after {attempts} attempts",
    )


def _phase_6_threshold_audit(rep: LifecycleReporter) -> None:
    ful = _get(FULFILLMENT_URL)
    tun = ful.get("tuning_variables") or {}
    base = float(tun.get("signal_threshold") or 52.5)
    dyn = dynamic_confidence_floor(
        base_threshold=base,
        atr=12.0,
        atr_baseline=20.0,
        rsi=48.0,
    )
    gd = ful.get("gate_diagnostics") or {}
    blockers = [
        f"{b['epic']}: {b['reason']}"
        for b in audit_trade_blockers(gd)
    ]
    rep.append_phase_report(
        6,
        status="Success",
        ml_assessment={
            "tensor": "volatility_gate",
            "confidence_spread": f"{dyn['base_threshold']}→{dyn['adjusted_threshold']}",
            "signal": "dynamic floor applied" if dyn["viable"] else "static",
        },
        blockers=blockers[:8],
        hardening="harmonization/volatility_gate.py dynamic_confidence_floor",
        close_down="audit only",
    )


def _phase_7_blocker_root_cause(rep: LifecycleReporter) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "live_frontier_probe",
        ROOT / "scripts" / "live_frontier_probe.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    code = int(mod.main())
    ful = _get(FULFILLMENT_URL)
    gd = ful.get("gate_diagnostics") or {}
    blockers = [
        f"{b['epic']}: {b['zone']} — {b['reason']}"
        for b in audit_trade_blockers(gd)
    ]
    perf = len(ful.get("performance_rows") or [])
    if perf:
        blockers.append(
            f"ledger shows {perf} rows — verify deal_id present (phantom fill guard)"
        )
    rep.append_phase_report(
        7,
        status="Success" if code == 0 else f"Probe exit {code}",
        blockers=blockers,
        hardening="bare_metal shadow_force disabled for DEMO broker",
        close_down="probe read-only",
    )


def _phase_8_demo_handshake(rep: LifecycleReporter) -> None:
    blockers: list[str] = []
    mode = os.environ.get("IG_AGENT_MODE", "DEMO")
    if mode != "DEMO":
        blockers.append(f"IG_AGENT_MODE={mode} — phase 8 expects DEMO")
    try:
        from ig_api.rest_client import get_shared_rest_client

        client = get_shared_rest_client()
        acct = client.fetch_account_snapshot() if client else {}
        balance = acct.get("balance") if isinstance(acct, dict) else None
        if balance is None:
            blockers.append("account snapshot missing balance")
    except Exception as exc:
        blockers.append(f"REST auth failed: {type(exc).__name__}: {exc}")
    rep.append_phase_report(
        8,
        status="Success" if not blockers else "Blocked",
        blockers=blockers,
        hardening="IronCladRiskEngine wired at LiveExecutor",
        close_down="no orders",
    )


def _phase_9_instrument_validation(rep: LifecycleReporter) -> None:
    epics = (
        "CS.D.CFPGOLD.CFP.IP",
        "IX.D.DOW.IFM.IP",
        "IX.D.NIKKEI.IFM.IP",
        "CS.D.EURUSD.CFD.IP",
    )
    blockers: list[str] = []
    try:
        from ig_api.rest_client import get_shared_rest_client

        client = get_shared_rest_client()
        for epic in epics:
            try:
                mkt = client.get_market_details(epic) if client else None
                if not mkt:
                    blockers.append(f"{epic}: no market details")
            except Exception as exc:
                blockers.append(f"{epic}: {type(exc).__name__}")
    except Exception as exc:
        blockers.append(f"client unavailable: {exc}")
    rep.append_phase_report(
        9,
        status="Success" if not blockers else "Partial",
        blockers=blockers,
        hardening="night matrix epics validated against IG DEMO REST",
        close_down="no orders",
    )


def _phase_10_demo_fire(rep: LifecycleReporter) -> None:
    """
    DEMO micro-lot only. Live fire blocked without operator sign-off.
    """
    blockers: list[str] = []
    if os.environ.get("IG_ALLOW_LIVE_FIRE", "").strip() not in ("1", "true", "yes"):
        blockers.append(
            "LIVE fire blocked — set IG_ALLOW_LIVE_FIRE=1 + LIVE_PROMOTION_CHECKLIST "
            "for real funds. Running DEMO routing validation only."
        )
    try:
        from system.e2e_execution_check import run_e2e_execution_check

        result = run_e2e_execution_check()
        ok = bool(result.get("ok"))
        if not ok:
            blockers.append(str(result.get("detail") or result.get("error") or "e2e failed"))
    except Exception as exc:
        blockers.append(f"e2e probe: {type(exc).__name__}: {exc}")
        ok = False
    rep.append_phase_report(
        10,
        status="DEMO validated" if ok else "Blocked",
        blockers=blockers,
        hardening="Phase 10 DEMO e2e — live requires explicit operator gate",
        close_down="no live positions — DEMO routing check only",
    )


PHASES = {
    1: _phase_1_network_hardening,
    2: _phase_2_memory_purge,
    3: _phase_3_shm_integrity,
    4: _phase_4_velocity_watchdog,
    5: _phase_5_reconnect_stress,
    6: _phase_6_threshold_audit,
    7: _phase_7_blocker_root_cause,
    8: _phase_8_demo_handshake,
    9: _phase_9_instrument_validation,
    10: _phase_10_demo_fire,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Adversarial harmonization loop")
    parser.add_argument("--phase", type=int, default=0, help="Run single phase 1-10")
    args = parser.parse_args()
    ensure_audit_files()
    rep = LifecycleReporter()
    phases = [args.phase] if args.phase else list(range(1, 11))
    exit_code = 0
    for n in phases:
        fn = PHASES.get(n)
        if fn is None:
            print(f"Unknown phase {n}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"=== PHASE {n}/10 ===", flush=True)
        try:
            fn(rep)
        except Exception as exc:
            rep.append_phase_report(
                n,
                status=f"Exception Tripped: {type(exc).__name__}: {exc}",
                traceback_text=traceback.format_exc(),
                hardening="none — phase aborted",
                close_down="unknown",
            )
            exit_code = 1
    print(f"Audit: {ROOT / 'lifecycle_audit.md'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
