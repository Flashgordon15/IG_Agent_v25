#!/usr/bin/env python3
"""15-minute witness: heartbeat file + sparse API probes (avoids thread-pool saturation)."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT = ROOT / "src/data/health_light_heartbeat.json"
DURATION_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 900
INTERVAL_SEC = 5.0
API_PROBE_EVERY = 12  # every 60s


def read_hb() -> tuple[str, int]:
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        return str(data.get("ts") or ""), int(data.get("pid") or 0)
    except Exception:
        return "", 0


def api_health_light(timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8080/api/health_light", timeout=timeout
        ) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main() -> int:
    start = time.monotonic()
    last_ts = ""
    samples = 0
    hb_ok = 0
    api_ok = 0
    fresh_ok = 0
    exec_ok = 0
    max_streak = 0
    streak = 0
    print(f"witness: {DURATION_SEC}s heartbeat={HEARTBEAT}", flush=True)
    while time.monotonic() - start < DURATION_SEC:
        samples += 1
        ts, pid = read_hb()
        hb_adv = bool(ts) and ts != last_ts
        if hb_adv:
            last_ts = ts
            hb_ok += 1
            streak = 0
        else:
            streak += 1
            max_streak = max(max_streak, streak)

        fresh = 0
        exec_active = False
        if samples % API_PROBE_EVERY == 0:
            body = api_health_light(4.0)
            if body:
                api_ok += 1
                hub = (body.get("data_feeds") or {}).get("hub") or {}
                fresh = int(hub.get("fresh_count") or 0)
                exec_active = bool(body.get("execution_loop_active"))
                if fresh >= 4:
                    fresh_ok += 1
                if exec_active:
                    exec_ok += 1

        ok = hb_adv and (fresh >= 4 if fresh else True)
        print(
            f"[{samples}] pid={pid} hb_adv={hb_adv} ts={ts[-19:] if ts else 'none'} "
            f"fresh={fresh if fresh else '-'} exec={exec_active if samples % API_PROBE_EVERY == 0 else '-'} "
            f"streak={streak}",
            flush=True,
        )
        time.sleep(INTERVAL_SEC)

    hb_pct = hb_ok / samples * 100 if samples else 0
    print(
        f"done samples={samples} hb_adv={hb_ok} ({hb_pct:.1f}%) api_ok={api_ok} "
        f"fresh_ok={fresh_ok} exec_ok={exec_ok} max_streak={max_streak}",
        flush=True,
    )
    passed = hb_pct >= 95 and max_streak <= 3 and (api_ok == 0 or fresh_ok >= api_ok * 0.8)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
