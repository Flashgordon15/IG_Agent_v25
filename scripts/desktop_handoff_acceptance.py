#!/usr/bin/env python3
"""
Desktop splash → dashboard handoff contract (CI + post-boot).

Validates iron_cage coupling invariants and, when the agent is live,
confirms launcher-ready + trade_ready would not block handoff.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from cockpit.agent_api_proxy import iron_cage_is_agent_coupled

API_PORT = 8080
COCKPIT_PORT = 8787


def _fetch(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _check_invariants() -> list[str]:
    failures: list[str] = []

    cases = (
        ("trade_ready+gates_none", {"trade_ready": True, "gates": None}, {"trade_ready": True, "gates": None}, True),
        ("agent_trade_ready", {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}, {"trade_ready": True, "gates": None}, True),
        ("orphan_stub", {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}, {"trade_ready": False, "gates": {"G1": {"status": "pending"}}}, False),
        ("local_gate_complete", {"trade_ready": False, "gates": {"G1": {"status": "complete"}}}, None, True),
    )
    for name, local, agent, expected in cases:
        got = iron_cage_is_agent_coupled(local, agent)
        if got is not expected:
            failures.append(f"invariant:{name} expected={expected} got={got}")

    return failures


def _check_live_handoff() -> list[str]:
    failures: list[str] = []
    local = _fetch(f"http://127.0.0.1:{COCKPIT_PORT}/api/iron_cage_status")
    agent = _fetch(f"http://127.0.0.1:{API_PORT}/api/iron_cage_status")
    if local is None and agent is None:
        return failures

    if not iron_cage_is_agent_coupled(local, agent):
        failures.append("live:coupling_false_while_agent_up")

    launcher_ready = False
    try:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        status_path = root / "logs" / "launcher_status.json"
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            stage = str(status.get("stage") or "").lower()
            step = int(status.get("step") or 0)
            launcher_ready = stage in ("gui", "ready") or step >= 8
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        launcher_ready = False

    trade_ready = bool((local or {}).get("trade_ready")) or bool((agent or {}).get("trade_ready"))
    if launcher_ready and trade_ready and not iron_cage_is_agent_coupled(local, agent):
        failures.append("live:launcher_ready_trade_ready_but_not_coupled")

    return failures


def main() -> int:
    failures = _check_invariants()
    failures.extend(_check_live_handoff())
    if failures:
        for row in failures:
            print(f"FAIL {row}", file=sys.stderr)
        return 1
    print("desktop_handoff_acceptance OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
