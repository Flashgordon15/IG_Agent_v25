#!/usr/bin/env python3
"""10-minute UI stress monitor — polls agent health + fulfillment every 60s."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "src" / "data" / "logs"
TICKS = 10
INTERVAL_SEC = 60.0


def _fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _ui_code() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:3000/", timeout=5) as resp:
            return str(resp.status)
    except Exception:
        return "down"


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"ui_stress_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    lines: list[str] = []
    header = "=== UI STRESS HOT RESTART MONITOR — 10 min @ 60s ==="
    print(header, flush=True)

    # Wait up to 3 min for agent bind
    for _ in range(90):
        if _fetch("http://127.0.0.1:8080/api/health"):
            break
        time.sleep(2)

    # Arm stress once API is live
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8080/api/internal/ui-stress-render",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        pass

    lines.append(header)
    for tick in range(1, TICKS + 1):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        health = _fetch("http://127.0.0.1:8080/api/health")
        full = _fetch("http://127.0.0.1:8080/api/unified/fulfillment")
        ui = _ui_code()
        bm = health.get("boot_metrics") or {}
        g5 = ((bm.get("system_state") or {}).get("gates") or {}).get("G5", {})
        stress = full.get("ui_stress_render") or {}
        gold = (full.get("market_quotes") or {}).get("CS.D.CFPGOLD.CFP.IP") or {}
        line = (
            f"tick={tick} ts={ts} alive={health.get('agent_alive')} "
            f"pid={health.get('agent_pid')} boot={bm.get('percent')} "
            f"G5={g5.get('status')} stress_active={stress.get('active')} "
            f"stress_hz={stress.get('hz')} gold_mid={round(float(gold.get('mid') or 0), 2)} "
            f"gold_src={gold.get('source')} pulse={full.get('pulse_serial')} ui3000={ui}"
        )
        print(line, flush=True)
        lines.append(line)
        if tick < TICKS:
            time.sleep(INTERVAL_SEC)
    footer = f"=== MONITOR COMPLETE — log: {log_path} ==="
    print(footer)
    lines.append(footer)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
