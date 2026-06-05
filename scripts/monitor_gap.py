#!/usr/bin/env python3
"""Live gap-countdown and trade-readiness monitor. Prints every 30s."""

import sys
import time
from datetime import datetime

sys.path.insert(0, "/Users/chrisgordon/Desktop/IG_Agent_v25/src")
from api.snapshot_store import get_tick  # noqa: E402

SKIP = {"Germany 40"}

while True:
    try:
        tick = get_tick()
        markets = tick.get("markets", {})
        now = datetime.now().strftime("%H:%M:%S")
        for m in markets.values():
            mkt = m.get("market", "")
            if mkt in SKIP:
                continue
            gates = m.get("health", {}).get("gates", [])
            sig = m.get("signal", {})
            gap = next((g for g in gates if g["name"] == "cold_start_gap"), {})
            bars = gap.get("value", {}).get("bars", 0)
            gap_pass = gap.get("pass", False)
            conf = sig.get("confidence", 0)
            thresh = sig.get("threshold", 80)
            remaining = max(0, 12 - int(bars))
            gap_str = (
                "CLEAR" if gap_pass else f"BLOCKED {bars}/12 (~{remaining * 5}min)"
            )
            flag = ""
            if gap_pass:
                flag = " <<< GAP CLEARED"
            if gap_pass and conf >= thresh:
                flag = " <<< TRADE READY"
            print(f"[{now}] {mkt}: gap={gap_str} | conf={conf}% need={thresh}%{flag}")
        print()
    except Exception as e:
        print(f"monitor error: {e}")
    time.sleep(30)
