#!/usr/bin/env python3
"""Live E2E API audit — run against a healthy agent on localhost:8080."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://localhost:8080"


def call(
    method: str, path: str, body: dict | None = None, timeout: float = 130
) -> dict[str, Any]:
    url = f"{BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"_raw": raw[:300]}
            return {
                "path": path,
                "method": method,
                "status": resp.status,
                "ok": 200 <= resp.status < 300,
                "json": parsed,
            }
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw[:300]}
        return {
            "path": path,
            "method": method,
            "status": e.code,
            "ok": False,
            "json": parsed,
        }
    except Exception as e:
        return {
            "path": path,
            "method": method,
            "status": 0,
            "ok": False,
            "error": str(e),
        }


def main() -> int:
    results: list[dict[str, Any]] = []

    # Read-only GETs
    for method, path in [
        ("GET", "/health"),
        ("GET", "/api/health"),
        ("GET", "/api/startup/status"),
        ("GET", "/state"),
        ("GET", "/api/splash"),
        ("GET", "/api/trades"),
        ("GET", "/api/signals"),
        ("GET", "/api/system"),
        ("GET", "/api/replay/summary"),
        ("GET", "/api/shadow/today"),
        ("GET", "/api/learning/status"),
        ("GET", "/api/shutdown/verify-status"),
    ]:
        results.append(call(method, path))

    # POST actions (non-destructive where possible)
    results.append(call("POST", "/api/heartbeat", {}))
    results.append(call("POST", "/api/safe-to-leave", {}))
    results.append(call("POST", "/api/flatten/all", {}))
    results.append(call("POST", "/api/trades/reconcile", {}))
    results.append(call("POST", "/api/system/tests", {}))
    results.append(call("POST", "/api/system/e2e", {}))

    # Replay run (can be slow)
    replay = call("POST", "/api/replay/run", {}, timeout=300)
    results.append(replay)

    fails = [r for r in results if not r.get("ok")]
    print("LIVE E2E API AUDIT")
    print("=" * 60)
    for r in results:
        status = r.get("status", "?")
        mark = "PASS" if r.get("ok") else "FAIL"
        extra = ""
        j = r.get("json") or {}
        if r["path"] == "/api/safe-to-leave":
            extra = f" ok={j.get('ok')} checks={len(j.get('checks', []))}"
        elif r["path"] == "/api/replay/run":
            extra = (
                f" ok={j.get('ok')} bars={j.get('bars_processed', j.get('bars', '?'))}"
            )
        elif r["path"] == "/api/flatten/all":
            extra = f" count={j.get('count', '?')}"
        elif r["path"] == "/api/system/tests":
            extra = f" passed={j.get('passed', '?')} failed={j.get('failed', '?')}"
        elif r["path"] == "/api/health":
            extra = f" trading_healthy={j.get('trading_healthy')}"
        elif r["path"] == "/state":
            extra = f" markets={len((j.get('markets') or {}))} closed_trades={len(j.get('closed_trades') or [])}"
        if r.get("error"):
            extra = f" error={r['error']}"
        if not r.get("ok") and j.get("_raw"):
            extra += f" raw={j['_raw'][:80]}"
        print(f"[{mark}] {r['method']} {r['path']} HTTP {status}{extra}")

    print("=" * 60)
    print(f"Total: {len(results)}  Failed: {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
