#!/usr/bin/env python3
"""Live frontier + gate probe — read-only diagnostics against running agent."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EPIC_LABEL = {
    "CS.D.CFPGOLD.CFP.IP": "Gold",
    "IX.D.DOW.IFM.IP": "Wall St",
    "IX.D.NIKKEI.IFM.IP": "Nikkei",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
}


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    print("=== Live Frontier + Gate Probe ===")
    try:
        health = _get("http://127.0.0.1:8080/api/health")
    except Exception as exc:
        print(f"FAIL: agent not reachable — {exc}")
        return 1

    print(f"Agent PID {health.get('agent_pid')} ready={health.get('boot_metrics', {}).get('ready')}")

    try:
        snap = _get("http://127.0.0.1:8080/api/unified/fulfillment")
    except Exception as exc:
        print(f"FAIL: fulfillment API — {exc}")
        return 1

    tun = snap.get("tuning_variables") or {}
    print(f"Tuning: threshold={tun.get('signal_threshold')} atr={tun.get('atr_multiplier')} source={tun.get('source')}")
    dv = snap.get("data_velocity") or {}
    print(f"Velocity: ticks={dv.get('ticks_cached')} live={dv.get('live_ram_ticks')} stall={dv.get('stall_active')}")

    ft = snap.get("alpha_frontier_tracker") or {}
    ring = ft.get("ring") or {}
    print(f"Ring: zone={ring.get('last_zone_label')} injecting={ring.get('injecting')} win_armed={ring.get('win_zone_armed')}")

    print("\nPer-market frontier:")
    blocked = 0
    for epic, row in sorted((ft.get("by_epic") or {}).items()):
        label = EPIC_LABEL.get(epic, epic)
        wait = row.get("wait_reason") or "—"
        if wait and wait not in ("", "PASS") and "execution" in wait.lower() or "ALPHA" in wait or "FAIL" in wait:
            blocked += 1
        print(
            f"  {label:8} zone={row.get('zone_label'):10} dir={row.get('direction'):4} "
            f"wait={wait}"
        )

    print(f"\nSummary: {blocked} blocked / {len(ft.get('by_epic') or {})} markets")
    print(f"Performance rows: {len(snap.get('performance_rows') or [])}")

    try:
        from system.e2e_execution_check import run_demo_routing_check

        route = run_demo_routing_check()
        if route.get("ok"):
            print(
                f"\nDEMO routing OK epic={route.get('epic')} "
                f"bid/offer={route.get('bid')}/{route.get('offer')}"
            )
        else:
            print(f"\nDEMO routing FAIL: {route.get('error')}")
            return 1
    except Exception as exc:
        print(f"\nDEMO routing skip: {exc}")

    if blocked and not snap.get("performance_rows"):
        print("\nRESULT: gates blocking entries — see wait_reason per market above")
        return 2
    print("\nRESULT: probe complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
