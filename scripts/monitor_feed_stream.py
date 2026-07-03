#!/usr/bin/env python3
"""Monitor health_light + hub freshness for N minutes — exit 0 if continuous."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DURATION_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 900
INTERVAL_SEC = 5.0
MIN_FRESH = 4


def fetch(path: str, timeout: float = 8.0) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    start = time.monotonic()
    last_hb = ""
    stale_streak = 0
    max_stale = 0
    samples = 0
    ok_samples = 0
    print(f"monitor_feed_stream: {DURATION_SEC}s interval={INTERVAL_SEC}s", flush=True)
    while time.monotonic() - start < DURATION_SEC:
        samples += 1
        try:
            hl = fetch("/api/health_light", 5.0)
            hb = str(hl.get("heartbeat_ts") or "")
            hub = (hl.get("data_feeds") or {}).get("hub") or {}
            fresh = int(hub.get("fresh_count") or 0)
            exec_ok = bool(hl.get("execution_loop_active"))
            hb_ok = bool(hb) and hb != last_hb
            if hb_ok:
                last_hb = hb
            sample_ok = hb_ok and fresh >= MIN_FRESH and exec_ok
            if sample_ok:
                ok_samples += 1
                stale_streak = 0
            else:
                stale_streak += 1
                max_stale = max(max_stale, stale_streak)
            print(
                f"[{samples}] hb_adv={hb_ok} fresh={fresh}/7 exec={exec_ok} "
                f"streak={stale_streak} hb={hb[-19:] if hb else 'none'}",
                flush=True,
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            stale_streak += 1
            max_stale = max(max_stale, stale_streak)
            print(f"[{samples}] ERROR {type(exc).__name__}: {exc}", flush=True)
        time.sleep(INTERVAL_SEC)
    pct = (ok_samples / samples * 100.0) if samples else 0.0
    print(
        f"done samples={samples} ok={ok_samples} ({pct:.1f}%) max_stale_streak={max_stale}",
        flush=True,
    )
    return 0 if ok_samples >= samples * 0.95 and max_stale <= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
