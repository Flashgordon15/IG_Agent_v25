#!/usr/bin/env python3
"""Profile /api/health and /api/gui_status latency under concurrent load."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fetch(url: str, timeout: float) -> tuple[float, int, dict | None]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                payload = None
            return elapsed_ms, int(resp.status), payload
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = None
        return elapsed_ms, int(exc.code), payload
    except Exception:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return elapsed_ms, 0, None


def _summarize(name: str, samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(samples)
    n = len(ordered)
    return {
        "count": float(n),
        "p50_ms": round(ordered[n // 2], 2),
        "p95_ms": round(ordered[int(n * 0.95)] if n > 1 else ordered[-1], 2),
        "max_ms": round(ordered[-1], 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile readiness endpoints")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    endpoints = {
        "health": f"{base}/api/health",
        "gui_status": f"{base}/api/gui_status",
        "profile": f"{base}/api/readiness/profile",
    }

    results: dict[str, list[float]] = {k: [] for k in ("health", "gui_status")}
    errors = 0

    def _one(label: str, url: str) -> None:
        nonlocal errors
        ms, code, _payload = _fetch(url, args.timeout)
        results[label].append(ms)
        if code == 0 or ms > 200.0:
            errors += 1

    urls = [endpoints["health"], endpoints["gui_status"]] * (args.requests // 2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        for i, url in enumerate(urls):
            label = "health" if "health" in url else "gui_status"
            futures.append(pool.submit(_one, label, url))
        concurrent.futures.wait(futures)

    _, _, profile = _fetch(endpoints["profile"], args.timeout)
    report = {
        "health": _summarize("health", results["health"]),
        "gui_status": _summarize("gui_status", results["gui_status"]),
        "slow_or_failed": errors,
        "server_profile": profile,
    }
    print(json.dumps(report, indent=2))

    health_p95 = report["health"]["p95_ms"]
    gui_p95 = report["gui_status"]["p95_ms"]
    if health_p95 > 200 or gui_p95 > 200:
        print(
            f"\nWARN: p95 exceeds 200ms target (health={health_p95} gui={gui_p95})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
