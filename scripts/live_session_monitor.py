#!/usr/bin/env python3
"""Sample agent health/trading endpoints during a live soak window."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

BASE = "http://127.0.0.1:8080"


def _get(path: str, timeout: float = 5.0) -> tuple[float, int, dict[str, Any] | None, str | None]:
    url = f"{BASE}{path}"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            try:
                return elapsed_ms, resp.status, json.loads(body), None
            except json.JSONDecodeError:
                return elapsed_ms, resp.status, None, "invalid_json"
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return elapsed_ms, exc.code, None, str(exc)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return elapsed_ms, 0, None, f"{type(exc).__name__}: {exc}"


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sample_once() -> dict[str, Any]:
    row: dict[str, Any] = {"ts": _iso()}
    endpoints = [
        ("health", "/api/health"),
        ("health_light", "/api/health_light"),
        ("state", "/api/state"),
        ("gui_status", "/api/gui_status"),
        ("diagnostics", "/api/diagnostics"),
        ("telemetry", "/api/v31/telemetry"),
        ("trades", "/api/trades"),
    ]
    latencies: dict[str, float] = {}
    for key, path in endpoints:
        ms, code, data, err = _get(path)
        latencies[key] = round(ms, 2)
        row[f"{key}_ms"] = round(ms, 2)
        row[f"{key}_code"] = code
        if err:
            row[f"{key}_error"] = err
        if key == "health" and data:
            ss = data.get("system_state") or {}
            row["phase"] = ss.get("phase") or data.get("status")
            row["ready"] = data.get("boot_metrics", {}).get("ready", data.get("ready"))
        if key == "health_light" and data:
            row["exec_loop"] = data.get("execution_loop_active")
            row["feed_stall"] = data.get("feed_stall")
            row["feed_age_ms"] = data.get("feed_heartbeat_age_ms")
            row["routing_armed"] = (data.get("routing_state") or {}).get("armed")
            row["ig_ok"] = data.get("ig_available")
            row["yahoo_ok"] = data.get("yahoo_available")
            row["rotation_reason"] = data.get("last_rotation_reason")
            row["stack_tpm"] = data.get("stack_tpm")
        if key == "telemetry" and data:
            row["ticks_processed"] = data.get("ticks_processed")
            row["rotation_sweeps"] = data.get("rotation_sweep_count")
            row["stacked"] = [
                {
                    "epic": c.get("epic"),
                    "tpm": c.get("ticks_per_minute"),
                    "z": c.get("live_calculated_zscore"),
                }
                for c in (data.get("stacked_asset_channels") or [])
            ]
            row["positions_count"] = len(data.get("active_positions") or [])
        if key == "trades" and data:
            trades = data if isinstance(data, list) else data.get("trades") or []
            row["trade_count"] = len(trades)
            if trades:
                row["last_trade"] = trades[0] if isinstance(trades[0], dict) else str(trades[0])[:80]
        if key == "diagnostics" and data:
            ex = data.get("execution") or {}
            rt = data.get("routing") or {}
            row["loops_running"] = ex.get("loops_running")
            row["armed_count"] = rt.get("armed_count")
    row["latency_summary"] = latencies
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--out", default="logs/live_session_report.json")
    args = parser.parse_args()
    n = max(1, int(args.minutes * 60 / args.interval))
    samples: list[dict[str, Any]] = []
    print(f"live_session_monitor: {n} samples every {args.interval}s", flush=True)
    for i in range(n):
        row = sample_once()
        samples.append(row)
        print(
            f"[{i+1}/{n}] phase={row.get('phase')} ready={row.get('ready')} "
            f"tpm={row.get('stack_tpm')} trades={row.get('trade_count')} "
            f"health_light={row.get('health_light_ms')}ms stall={row.get('feed_stall')}",
            flush=True,
        )
        if i < n - 1:
            time.sleep(args.interval)
    # Aggregate
    def _collect(key: str) -> list[float]:
        return [float(s[key]) for s in samples if s.get(key) is not None]

    report = {
        "started": samples[0]["ts"] if samples else _iso(),
        "ended": samples[-1]["ts"] if samples else _iso(),
        "sample_count": len(samples),
        "interval_sec": args.interval,
        "latency_p50": {
            k: round(statistics.median(_collect(f"{k}_ms")), 2)
            for k in ("health", "health_light", "state", "gui_status", "diagnostics", "telemetry")
            if _collect(f"{k}_ms")
        },
        "latency_max": {
            k: round(max(_collect(f"{k}_ms")), 2)
            for k in ("health", "health_light", "state", "gui_status", "diagnostics", "telemetry")
            if _collect(f"{k}_ms")
        },
        "feed_stall_samples": sum(1 for s in samples if s.get("feed_stall")),
        "exec_loop_active_samples": sum(1 for s in samples if s.get("exec_loop")),
        "trade_count_delta": (
            (samples[-1].get("trade_count") or 0) - (samples[0].get("trade_count") or 0)
            if samples
            else 0
        ),
        "ticks_delta": (
            (samples[-1].get("ticks_processed") or 0) - (samples[0].get("ticks_processed") or 0)
            if samples
            else 0
        ),
        "rotation_sweep_delta": (
            (samples[-1].get("rotation_sweeps") or 0) - (samples[-1].get("rotation_sweeps") or 0)
            if samples
            else 0
        ),
        "samples": samples,
    }
    if len(samples) >= 2:
        report["rotation_sweep_delta"] = (
            (samples[-1].get("rotation_sweeps") or 0) - (samples[0].get("rotation_sweeps") or 0)
        )
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"report written: {out_path}", flush=True)
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
