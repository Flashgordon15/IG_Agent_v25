#!/usr/bin/env python3
"""
view_active_gates.py — read-only live gate matrix probe for v31 sandbox.

Polls the active agent HTTP API (default :8081) every 1s and renders per-asset
gate pass/fail state. No imports from src/ — stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Canonical 12-gate schema (api/snapshot.py GATE_NAMES)
GATE_NAMES: tuple[str, ...] = (
    "session_open",
    "session_blackout",
    "cold_start_gap",
    "environment_fitness",
    "points_state",
    "correlation_ok",
    "risk_validation",
    "expectancy_ok",
    "calendar_ok",
    "signal_confidence",
    "ml_veto",
    "execution",
)

# Alpha-matrix hot path gates (compact telemetry on v31 sandbox)
ALPHA_GATES: tuple[str, ...] = (
    "alpha_matrix_lookup",
    "alpha_matrix_approved",
)

ACTIVE_ASSETS: tuple[tuple[str, str], ...] = (
    ("IX.D.DOW.IFM.IP", "Wall St"),
    ("IX.D.NASDAQ.IFM.IP", "Nasdaq"),
    ("CS.D.CFPGOLD.CFP.IP", "Gold"),
)

GATE_COLUMNS: tuple[str, ...] = GATE_NAMES + ALPHA_GATES

GATE_LABELS: dict[str, str] = {
    "session_open": "sess_open",
    "session_blackout": "blackout",
    "cold_start_gap": "cold_gap",
    "environment_fitness": "env_fit",
    "points_state": "points",
    "correlation_ok": "corr",
    "risk_validation": "risk",
    "expectancy_ok": "expect",
    "calendar_ok": "calendar",
    "signal_confidence": "signal",
    "ml_veto": "ml_veto",
    "execution": "exec",
    "alpha_matrix_lookup": "α_lookup",
    "alpha_matrix_approved": "α_approve",
}

DEFAULT_API_BASE = os.environ.get(
    "IG_AGENT_API_BASE",
    os.environ.get("IG_V31_API_BASE", "http://127.0.0.1:8081"),
).rstrip("/")
POLL_SEC = float(os.environ.get("IG_GATE_PROBE_POLL_SEC", "1.0"))
HTTP_TIMEOUT_SEC = float(os.environ.get("IG_GATE_PROBE_TIMEOUT_SEC", "3.0"))

GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _http_json(path: str) -> dict[str, Any] | None:
    url = f"{DEFAULT_API_BASE}{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _gate_passed(row: dict[str, Any]) -> bool | None:
    if "pass" in row:
        return bool(row.get("pass"))
    if "passed" in row:
        return bool(row.get("passed"))
    return None


def _gate_detail(row: dict[str, Any]) -> str:
    for key in ("detail", "why_failed", "gating_reason"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    value = row.get("value")
    if isinstance(value, dict):
        reason = str(value.get("reason") or "").strip()
        if reason:
            return reason
    return ""


def _normalize_gate_map(gates: list[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in gates or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            out[name] = item
    return out


def _gates_for_epic(
    state: dict[str, Any] | None,
    fulfillment: dict[str, Any] | None,
    epic: str,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    if fulfillment:
        gd = fulfillment.get("gate_diagnostics") or {}
        if isinstance(gd, dict):
            by_epic = gd.get("by_epic") or {}
            if isinstance(by_epic, dict):
                row = by_epic.get(epic)
                if isinstance(row, dict):
                    merged.update(_normalize_gate_map(row.get("gates") or []))

    if state:
        markets = state.get("markets") or {}
        if isinstance(markets, dict):
            market = markets.get(epic)
            if isinstance(market, dict):
                health = market.get("health") or {}
                if isinstance(health, dict):
                    merged.update(_normalize_gate_map(health.get("gates") or []))

    return merged


def _health_badge(state: dict[str, Any] | None, epic: str) -> str:
    if not state:
        return "OFFLINE"
    markets = state.get("markets") or {}
    if not isinstance(markets, dict):
        return "—"
    market = markets.get(epic)
    if not isinstance(market, dict):
        return "—"
    health = market.get("health") or {}
    if not isinstance(health, dict):
        return "—"
    return str(health.get("badge") or "—")


def _symbol(passed: bool | None) -> str:
    if passed is True:
        return f"{GREEN}[✓]{RESET}"
    if passed is False:
        return f"{RED}[✗]{RESET}"
    return f"{DIM}[·]{RESET}"


def _clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _collect_bottlenecks(
    matrix: dict[str, dict[str, dict[str, Any]]],
) -> list[tuple[str, str, str]]:
    """Return (asset_label, gate_name, detail) for each failing gate."""
    blockers: list[tuple[str, str, str]] = []
    for epic, label in ACTIVE_ASSETS:
        gates = matrix.get(epic) or {}
        for gate_name in GATE_COLUMNS:
            row = gates.get(gate_name)
            if not row:
                continue
            passed = _gate_passed(row)
            if passed is False:
                detail = _gate_detail(row) or "gate blocked"
                blockers.append((label, gate_name, detail))
    return blockers


def _render(
    *,
    state: dict[str, Any] | None,
    fulfillment: dict[str, Any] | None,
    health: dict[str, Any] | None,
    matrix: dict[str, dict[str, dict[str, Any]]],
    error: str = "",
) -> None:
    _clear_screen()
    agent_pid = (health or {}).get("agent_pid", "?")
    boot = (health or {}).get("boot_metrics") or {}
    ready = boot.get("ready", (health or {}).get("trading_healthy"))
    print(f"{BOLD}IG Agent v31 — Live Gate Memory Probe{RESET}")
    print(
        f"API {DEFAULT_API_BASE}  |  PID {agent_pid}  |  ready={ready}  |  {_iso_now()}"
    )
    if error:
        print(f"{RED}FETCH ERROR: {error}{RESET}")
    print()

    header = f"{'Asset':<10}"
    for gate in GATE_COLUMNS:
        header += f" {GATE_LABELS.get(gate, gate)[:9]:>9}"
    print(header)
    print("-" * len(header))

    for epic, label in ACTIVE_ASSETS:
        row = f"{label:<10}"
        gates = matrix.get(epic) or {}
        for gate_name in GATE_COLUMNS:
            gate_row = gates.get(gate_name)
            if gate_row is None:
                row += f" {_symbol(None):>13}"
            else:
                row += f" {_symbol(_gate_passed(gate_row)):>13}"
        badge = _health_badge(state, epic)
        print(f"{row}  {DIM}{badge}{RESET}")

    print()
    print(f"{BOLD}Gate legend:{RESET} {GREEN}[✓]{RESET} pass  {RED}[✗]{RESET} fail  {DIM}[·]{RESET} not in live snapshot")
    print(f"{DIM}Sources: GET /state → markets[epic].health.gates + GET /api/unified/fulfillment → gate_diagnostics{RESET}")
    print()

    blockers = _collect_bottlenecks(matrix)
    print(f"{BOLD}CURRENT ENTRY BOTTLENECK{RESET}")
    if not blockers:
        if state is None:
            print(f"{DIM}  (waiting for API — no gate memory yet){RESET}")
        else:
            print(f"  {GREEN}None — all reported gates passing for monitored assets{RESET}")
    else:
        for asset, gate_name, detail in blockers:
            print(f"  {RED}✗{RESET} {asset} → {BOLD}{gate_name}{RESET}: {detail}")


def main() -> int:
    print(f"Starting gate probe on {DEFAULT_API_BASE} (Ctrl+C to exit)...", flush=True)
    time.sleep(0.5)
    while True:
        state = _http_json("/state")
        fulfillment = _http_json("/api/unified/fulfillment")
        health = _http_json("/api/health")

        error = ""
        if state is None and fulfillment is None:
            error = f"unreachable at {DEFAULT_API_BASE}"

        matrix: dict[str, dict[str, dict[str, Any]]] = {}
        for epic, _label in ACTIVE_ASSETS:
            matrix[epic] = _gates_for_epic(state, fulfillment, epic)

        _render(
            state=state,
            fulfillment=fulfillment,
            health=health,
            matrix=matrix,
            error=error,
        )
        time.sleep(max(0.2, POLL_SEC))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nGate probe stopped.")
        raise SystemExit(0)
