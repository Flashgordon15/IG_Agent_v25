#!/usr/bin/env python3
"""
Live HTTP/IPC pressure harness for shadow desktop sidecar (:9090 default).

Hammer health, state, and readiness endpoints concurrently; optional UDS ping.

Usage:
  PYTHONPATH=src python3 scripts/live_chaos_pressure.py
  PYTHONPATH=src python3 scripts/live_chaos_pressure.py --port 9090 --workers 32 --seconds 15
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@dataclass
class PressureResult:
    endpoint: str
    ok: int = 0
    fail: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _http_get(url: str, timeout: float) -> tuple[int, float, str]:
    t0 = time.perf_counter()
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(65536).decode("utf-8", errors="replace")
        code = int(getattr(resp, "status", 200))
    return code, (time.perf_counter() - t0) * 1000.0, body


def _uds_ping(sock_path: Path, timeout: float) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(b'{"op":"ping"}\n')
            data = sock.recv(4096)
        return True, (time.perf_counter() - t0) * 1000.0, data.decode("utf-8", errors="replace")[:200]
    except OSError as exc:
        return False, (time.perf_counter() - t0) * 1000.0, str(exc)


def _worker(
    *,
    base: str,
    path: str,
    timeout: float,
    stop_at: float,
    sock_path: Path | None,
) -> PressureResult:
    res = PressureResult(endpoint=path or "uds")
    while time.monotonic() < stop_at:
        try:
            if path:
                code, ms, _ = _http_get(f"{base}{path}", timeout)
                if 200 <= code < 300:
                    res.ok += 1
                    res.latencies_ms.append(ms)
                else:
                    res.fail += 1
                    if len(res.errors) < 5:
                        res.errors.append(f"HTTP {code}")
            elif sock_path is not None:
                ok, ms, detail = _uds_ping(sock_path, timeout)
                if ok:
                    res.ok += 1
                    res.latencies_ms.append(ms)
                else:
                    res.fail += 1
                    if len(res.errors) < 5:
                        res.errors.append(detail)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            res.fail += 1
            if len(res.errors) < 5:
                res.errors.append(str(exc))
    return res


def _summarize(results: list[PressureResult]) -> dict:
    out: dict = {"vectors": [], "passed": True}
    for r in results:
        p50 = statistics.median(r.latencies_ms) if r.latencies_ms else None
        p95 = (
            sorted(r.latencies_ms)[max(0, int(len(r.latencies_ms) * 0.95) - 1)]
            if r.latencies_ms
            else None
        )
        total = r.ok + r.fail
        fail_rate = (r.fail / total) if total else 1.0
        passed = fail_rate <= 0.05 and r.ok >= 10
        if not passed:
            out["passed"] = False
        out["vectors"].append(
            {
                "endpoint": r.endpoint,
                "ok": r.ok,
                "fail": r.fail,
                "fail_rate_pct": round(fail_rate * 100, 2),
                "p50_ms": round(p50, 2) if p50 is not None else None,
                "p95_ms": round(p95, 2) if p95 is not None else None,
                "passed": passed,
                "sample_errors": r.errors[:3],
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Live chaos pressure against sidecar HTTP")
    parser.add_argument("--port", type=int, default=int(__import__("os").environ.get("IG_API_PORT", "9090")))
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--timeout", type=float, default=2.5)
    parser.add_argument("--uds", action="store_true", help="Include apex_ipc.sock ping vector")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    paths = ("/api/health", "/api/state", "/api/startup/status")
    stop_at = time.monotonic() + args.seconds

    sock_path: Path | None = None
    if args.uds:
        from system.node_profile import get_node_profile

        sock_path = get_node_profile().ipc_socket

    jobs: list[tuple[str, str | None]] = [(p, p) for p in paths]
    if sock_path is not None:
        jobs.append(("", None))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                _worker,
                base=base,
                path=path if path else "",
                timeout=args.timeout,
                stop_at=stop_at,
                sock_path=sock_path if not path else None,
            )
            for path, _ in jobs
            for _ in range(max(1, args.workers // len(jobs)))
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    merged: dict[str, PressureResult] = {}
    for r in results:
        bucket = merged.setdefault(r.endpoint, PressureResult(endpoint=r.endpoint))
        bucket.ok += r.ok
        bucket.fail += r.fail
        bucket.latencies_ms.extend(r.latencies_ms)
        bucket.errors.extend(r.errors)

    report = _summarize(list(merged.values()))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
