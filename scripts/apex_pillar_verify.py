#!/usr/bin/env python3
"""v30 Apex live pillar verification — polls shadow API + isolated runtime artifacts."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_PORT = int(os.environ.get("IG_API_PORT", "9090"))
API_BASE = f"http://127.0.0.1:{API_PORT}"
ISOLATED_ROOT = Path.home() / "Library/Application Support/IG Agent Apex/v30-production"
DATA_DIR = Path(os.environ.get("IG_AGENT_DATA_DIR", str(ISOLATED_ROOT / "data")))
ANALYTICS_DB = Path(
    os.environ.get(
        "IG_TRIAGE_DB",
        str(ISOLATED_ROOT / "analytics/triage_v30.db"),
    )
)
LOG_FILE = DATA_DIR / "logs/shadow_v30.log"
IPC_SOCK = DATA_DIR / "apex_ipc.sock"


def _fetch(path: str, timeout: float = 8.0) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify() -> dict:
    results: dict = {"pillars": {}, "repairs": [], "ok": True}

    # Pillar 2 — ports & IPC
    p2: dict = {"checks": []}
    try:
        health = _fetch("/api/health")
        p2["api_health"] = health.get("status") or "ok"
        p2["checks"].append("api_health")
    except Exception as exc:
        p2["api_health_error"] = str(exc)
        results["ok"] = False

    try:
        boot = _fetch("/api/startup/status")
        p2["boot_ready"] = bool(boot.get("ready"))
        p2["boot_percent"] = boot.get("percent")
        if not p2["boot_ready"]:
            results["ok"] = False
        p2["checks"].append("startup_status")
    except Exception as exc:
        p2["boot_error"] = str(exc)
        results["ok"] = False

    p2["ipc_socket"] = IPC_SOCK.exists()
    if IPC_SOCK.exists():
        p2["ipc_socket_path"] = str(IPC_SOCK)
    else:
        results["ok"] = False
    p2["checks"].append("ipc_socket")

    # Pillar 5 — microkernel / ML veto floor
    p5: dict = {"checks": []}
    log_tail = ""
    log_full = ""
    if LOG_FILE.is_file():
        log_full = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        log_tail = log_full[-12000:]
    p5["log_present"] = bool(log_full)
    p5["microkernel_online"] = "micro-kernel" in log_full.lower() or "microkernel" in log_full.lower()
    p5["ipc_bridge_online"] = "IPC bridge listening" in log_full
    if not p5["microkernel_online"] and not p5["ipc_bridge_online"]:
        results["ok"] = False
    p5["checks"].append("microkernel_log")

    try:
        from signals.indicators import ML_VETO_FLOOR

        p5["ml_veto_floor"] = float(ML_VETO_FLOOR)
        p5["ml_veto_ok"] = abs(p5["ml_veto_floor"] - 0.450) < 1e-6
        if not p5["ml_veto_ok"]:
            results["ok"] = False
    except Exception as exc:
        p5["ml_veto_error"] = str(exc)
        results["ok"] = False
    p5["checks"].append("ml_veto_floor")

    # Pillar 1 — risk hardening
    p1: dict = {"checks": []}
    try:
        from apex.hardening import (
            BASELINE_EQUITY_GBP,
            PER_ASSET_RISK_CAP_GBP,
            PORTFOLIO_RISK_CEILING_GBP,
            floor_contract_size,
        )

        p1["baseline_capital_gbp"] = float(BASELINE_EQUITY_GBP)
        p1["per_asset_cap_gbp"] = float(PER_ASSET_RISK_CAP_GBP)
        p1["portfolio_ceiling_gbp"] = float(PORTFOLIO_RISK_CEILING_GBP)
        p1["floor_contract_size"] = floor_contract_size(3.7)[0]
        p1["capital_ok"] = abs(p1["baseline_capital_gbp"] - 10000.0) < 1.0
        p1["floor_ok"] = p1["floor_contract_size"] == 3
        if not p1["capital_ok"] or not p1["floor_ok"]:
            results["ok"] = False
    except Exception as exc:
        p1["error"] = str(exc)
        results["ok"] = False
    p1["checks"].append("hardening")

    # Pillar 3 — execution promotion path
    p3: dict = {"checks": []}
    try:
        from trading.trading_loop import promote_high_confidence_signal

        p3["promote_fn"] = callable(promote_high_confidence_signal)
        state = _fetch("/api/state")
        loops = state.get("trading_loops") or state.get("loops") or state.get("epics") or {}
        p3["trading_loops"] = len(loops) if isinstance(loops, dict) else 0
        quotes = (
            state.get("quotes")
            or state.get("market")
            or state.get("hub_quotes")
            or state.get("snapshot", {}).get("quotes")
            or {}
        )
        p3["quote_epics"] = len(quotes) if isinstance(quotes, dict) else 0
        p3["gate_trace_live"] = "GATE CHECK" in log_full or "GATE_TRACE" in log_full
        if p3["trading_loops"] == 0 and p3["quote_epics"] == 0 and not p3.get("gate_trace_live"):
            results["ok"] = False
    except Exception as exc:
        p3["error"] = str(exc)
        results["ok"] = False
    p3["checks"].append("promote_fn")

    # Pillar 4 — async ledger
    p4: dict = {"checks": []}
    p4["db_path"] = str(ANALYTICS_DB)
    p4["db_exists"] = ANALYTICS_DB.is_file()
    if p4["db_exists"]:
        try:
            conn = sqlite3.connect(f"file:{ANALYTICS_DB}?mode=ro", uri=True, timeout=2.0)
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            p4["journal_mode"] = mode[0] if mode else None
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            p4["tables"] = tables[:12]
            conn.close()
        except Exception as exc:
            p4["db_error"] = str(exc)
    else:
        p4["db_note"] = "ledger file pending first Worker D flush"
    p4["checks"].append("triage_db")

    # Tick cycle evidence
    cycle: dict = {}
    try:
        health = _fetch("/api/health")
        cycle["quote_fresh"] = health.get("quotes_fresh") or health.get("quote_fresh")
        cycle["trading_loops_running"] = health.get("trading_loops_running")
        cycle["version"] = health.get("version") or health.get("app_version")
    except Exception:
        pass
    if log_tail:
        cycle["tick_ingest_lines"] = log_tail.lower().count("tick") + log_tail.lower().count("quote")

    results["pillars"] = {
        "pillar_1_risk": p1,
        "pillar_2_ports_ipc": p2,
        "pillar_3_execution": p3,
        "pillar_4_ledger": p4,
        "pillar_5_alpha": p5,
    }
    results["cycle"] = cycle
    return results


def main() -> int:
    os.environ.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    out = verify()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
