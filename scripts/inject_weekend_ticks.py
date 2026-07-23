#!/usr/bin/env python3
"""
Sandbox tick streamer — push synthetic price ticks into a running agent.

Requires ``sandbox_mode_enabled: true`` in config/config_v29.json (or via
the SANDBOX launcher profile).  The agent's POST /api/sim/tick endpoint
rejects all traffic when sandbox mode is off, so this script is inert
against a production instance.

Usage:
    python3 scripts/inject_weekend_ticks.py [--duration 120] [--cadence 0.5]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request

API_BASE = "http://127.0.0.1:8080"

INSTRUMENTS = {
    "IX.D.DOW.IFM.IP": {
        "label": "Wall Street",
        "base_price": 44250.0,
        "spread": 3.6,
        "amplitude": 45.0,
        "period_sec": 30.0,
    },
    "CS.D.CFPGOLD.CFP.IP": {
        "label": "Gold",
        "base_price": 2645.0,
        "spread": 0.4,
        "amplitude": 6.5,
        "period_sec": 25.0,
    },
}


def _post(path: str, payload: dict) -> dict | None:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 403:
            print(f"\n  BLOCKED: sandbox_mode_enabled is false on the running agent.", file=sys.stderr)
            print(f"  Enable it in config/config_v29.json or launch with the SANDBOX profile.\n", file=sys.stderr)
            sys.exit(1)
        print(f"  HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  connection error: {e}", file=sys.stderr)
        return None


def _synthetic_price(inst: dict, t: float) -> tuple[float, float]:
    base = inst["base_price"]
    amp = inst["amplitude"]
    period = inst["period_sec"]
    phase1 = math.sin(2 * math.pi * t / period)
    phase2 = math.sin(2 * math.pi * t / (period * 1.7) + 0.8) * 0.4
    drift = amp * (phase1 + phase2)
    mid = base + drift
    half_spread = inst["spread"] / 2
    return round(mid - half_spread, 2), round(mid + half_spread, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandbox tick streamer")
    parser.add_argument("--duration", type=int, default=120, help="seconds to stream (default 120)")
    parser.add_argument("--cadence", type=float, default=0.5, help="tick interval in seconds (default 0.5)")
    args = parser.parse_args()

    labels = ", ".join(i["label"] for i in INSTRUMENTS.values())
    print(f"[sandbox] Streaming mock ticks for {args.duration}s at {args.cadence}s cadence")
    print(f"[sandbox] Instruments: {labels}")
    print(f"[sandbox] Target: {API_BASE}/api/sim/tick")
    print()

    t0 = time.time()
    tick_count = 0
    fail_count = 0

    while time.time() - t0 < args.duration:
        elapsed = time.time() - t0
        for epic, inst in INSTRUMENTS.items():
            bid, offer = _synthetic_price(inst, elapsed)
            res = _post("/api/sim/tick", {"epic": epic, "bid": bid, "offer": offer})
            accepted = res and res.get("accepted")
            tick_count += 1
            tag = "OK" if accepted else ("ok" if res and res.get("ok") else "FAIL")
            if not accepted:
                fail_count += 1
            print(
                f"  [{elapsed:6.1f}s] {inst['label']:12s}  "
                f"B {bid:>10.2f} / O {offer:>10.2f}  [{tag}]"
            )

        time.sleep(args.cadence)

    print(f"\n[sandbox] Complete — {tick_count} ticks streamed, "
          f"{tick_count - fail_count} accepted, {fail_count} rejected")


if __name__ == "__main__":
    main()
