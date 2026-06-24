#!/usr/bin/env python3
"""
Trigger in-process alpha matrix compile on the live agent via HTTP.

Posts to POST /api/v1/alpha/compile so the running agent (not an external script)
allocates ig_agent_v30_alpha_matrix and runs secure_fill_matrix_update internally.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080/api/v1/alpha/compile"
POLL_URL = "http://127.0.0.1:8080/api/shadow/alpha-matrix"
EXPECTED_BYTES = 4_194_396
EXPECTED_SLOTS = 131_072


def _post_compile(url: str, *, stride: int, force: bool, timeout: float) -> dict:
    payload = json.dumps({"stride": stride, "force": force}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll_matrix(url: str, *, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="POST in-process alpha matrix compile to live agent"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--poll-url", default=POLL_URL)
    parser.add_argument("--stride", type=int, default=48)
    parser.add_argument("--no-force", action="store_true", help="Skip force recompile")
    parser.add_argument("--wait-sec", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    try:
        result = _post_compile(
            args.url,
            stride=max(8, args.stride),
            force=not args.no_force,
            timeout=args.timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"TRIGGER-COMPILE HTTP {exc.code}: {body[:500]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"TRIGGER-COMPILE failed: {exc}", file=sys.stderr)
        return 1

    print(f"TRIGGER-COMPILE accepted: {json.dumps(result, indent=2)}")

    if not result.get("accepted"):
        return 1

    deadline = time.monotonic() + max(5.0, float(args.wait_sec))
    while time.monotonic() < deadline:
        time.sleep(2.0)
        try:
            snap = _poll_matrix(args.poll_url, timeout=args.timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
        compilation = snap.get("compilation") or {}
        cells = int(compilation.get("cells_populated") or 0)
        status = str(compilation.get("status") or "")
        shm = (snap.get("hardware_ring_buffer") or {}).get("state")
        print(
            f"TRIGGER-COMPILE poll: cells={cells} status={status} shm={shm}"
        )
        if cells > 0 and status in ("ready", "compiling"):
            print(
                f"TRIGGER-COMPILE OK — cells={cells} "
                f"(target slots={EXPECTED_SLOTS} bytes={EXPECTED_BYTES})"
            )
            return 0

    print("TRIGGER-COMPILE WARN — compile accepted but cells still 0 after wait", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
