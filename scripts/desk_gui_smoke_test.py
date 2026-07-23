#!/usr/bin/env python3
"""Smoke barrage for Quantum Trading Desk GUI — :3000 routes + agent APIs."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

CFD = "http://127.0.0.1:8080"
SB = "http://127.0.0.1:8081"
UI = "http://127.0.0.1:3000"


def _fetch(url: str, *, timeout: float = 4.0) -> tuple[bool, Any]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype or url.endswith("/health"):
                try:
                    return True, json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    return True, body.decode("utf-8", errors="replace")[:500]
            return True, body.decode("utf-8", errors="replace")[:800]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def _check(name: str, url: str, *, predicate=None) -> bool:
    ok, payload = _fetch(url)
    if not ok:
        print(f"FAIL {name}: {payload}")
        return False
    if predicate and not predicate(payload):
        print(f"FAIL {name}: predicate rejected {str(payload)[:200]}")
        return False
    print(f"PASS {name}")
    return True


def main() -> int:
    results: list[bool] = []

    results.append(_check("ui_boot", f"{UI}/boot", predicate=lambda p: isinstance(p, str) and "<html" in p))
    results.append(_check("ui_desk", f"{UI}/desk", predicate=lambda p: isinstance(p, str) and len(p) > 200))
    results.append(_check("ui_root", f"{UI}/"))

    for label, base in (("cfd_health", CFD), ("sb_health", SB)):
        results.append(
            _check(
                label,
                f"{base}/api/health",
                predicate=lambda p: isinstance(p, dict) and p.get("agent_alive") is not None,
            )
        )

    for label, base, path in (
        ("cfd_ops_strip", CFD, "/api/desk/ops_strip"),
        ("cfd_positions_live", CFD, "/api/positions/live"),
        ("cfd_simplified_accounting", CFD, "/api/desk/simplified_accounting"),
        ("cfd_weekly_metrics", CFD, "/api/desk/weekly_metrics"),
        ("cfd_stability", CFD, "/api/desk/stability"),
    ):
        results.append(_check(label, f"{base}{path}"))

    ok_count = sum(1 for r in results if r)
    total = len(results)
    print(f"\nSMOKE SUMMARY: {ok_count}/{total} passed")
    return 0 if ok_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
