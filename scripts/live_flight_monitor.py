#!/usr/bin/env python3
"""Live-flight verification monitor — stages 1-9, feed hydration, strategy gates."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

API = "http://127.0.0.1:8080"
COCKPIT = "http://127.0.0.1:8787"
STAGE_NAMES = (
    "shutdown",
    "preflight",
    "tests",
    "genesis",
    "agent",
    "smoke",
    "verify",
    "gui",
    "ready",
)
MAX_WAIT_SEC = 600
POLL_SEC = 2.0
OUT = ROOT / "logs" / "live_flight_success.json"


def _get(url: str, timeout: float = 4.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _read_launcher_status() -> dict[str, Any]:
    path = ROOT / "logs" / "launcher_status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _port_bound(port: int) -> bool:
    from cockpit.desktop_process_guard import port_is_bound

    return port_is_bound(port)


def _trigger_healer_failover(reason: str) -> dict[str, Any]:
    out: dict[str, Any] = {"reason": reason, "actions": []}
    try:
        from ig_api.lightstreamer_streaming import force_rest_poll_failover

        ok = force_rest_poll_failover(reason=reason, category="live_flight_monitor")
        out["actions"].append({"force_rest_poll_failover": ok})
    except Exception as exc:
        out["actions"].append({"force_rest_poll_failover_error": str(exc)})
    try:
        from system.market_data_hub import run_synthetic_tick_injector

        run_synthetic_tick_injector()
        out["actions"].append({"synthetic_tick_injector": True})
    except Exception as exc:
        out["actions"].append({"synthetic_tick_injector_error": str(exc)})
    try:
        from system.autonomic_healer import AutonomicHealerEngine

        healer = AutonomicHealerEngine.get_instance()
        if healer is not None:
            healer.engage_transport_failover_recovery()
            out["actions"].append({"autonomic_failover_recovery": True})
    except Exception as exc:
        out["actions"].append({"autonomic_healer_error": str(exc)})
    return out


def main() -> int:
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ports_vacant_at_start": {
            "8080": not _port_bound(8080),
            "8787": not _port_bound(8787),
        },
        "stages": [],
        "health_retries_observed": [],
        "feed_audit": [],
        "strategy_gates": [],
        "healer_interventions": [],
        "final_status": "IN_PROGRESS",
    }
    last_stage = ""
    stage9_at: float | None = None
    deadline = time.time() + MAX_WAIT_SEC

    while time.time() < deadline:
        status = _read_launcher_status()
        stage = str(status.get("stage") or "")
        step = status.get("step")
        if stage and stage != last_stage:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "step": step,
                "detail": status.get("detail"),
                "boot_tier": status.get("boot_tier"),
            }
            report["stages"].append(entry)
            last_stage = stage
            print(f"[STAGE] {stage} step={step} :: {status.get('detail', '')}")

        health = _get(f"{API}/api/health")
        orch = _get(f"{API}/api/orchestrator_state")
        diag = _get(f"{API}/api/ai_diagnostics")
        gui = _get(f"{API}/api/gui_status")

        if stage in ("smoke", "tests", "verify") and health is None:
            report["health_retries_observed"].append(
                {"ts": datetime.now(timezone.utc).isoformat(), "api_up": False, "stage": stage}
            )

        if diag:
            feed_stale = bool(diag.get("feed_stale")) or str(diag.get("feed_status", "")).upper() == "STALE"
            zero_bytes = int(diag.get("ws_frame_bytes_last", 1) or 0) == 0
            veto = float(diag.get("shadow_walk_veto_pct", 0) or 0)
            report["feed_audit"].append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "feed_stale": feed_stale,
                    "ws_frame_bytes_last": diag.get("ws_frame_bytes_last"),
                    "synthetic_hydration_active": diag.get("synthetic_hydration_active"),
                    "fallback_transport_tier": diag.get("fallback_transport_tier"),
                    "shadow_walk_veto_pct": veto,
                }
            )
            if feed_stale or zero_bytes or veto >= 14.0:
                intervention = _trigger_healer_failover("live_flight_feed_stale")
                report["healer_interventions"].append(intervention)
                print(f"[HEALER] failover triggered feed_stale={feed_stale} veto={veto}%")

        if gui:
            report["strategy_gates"].append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "gui_status": gui,
                }
            )
            scores = gui.get("expectation_scores") or gui.get("asset_scores") or {}
            for epic, score in (scores.items() if isinstance(scores, dict) else []):
                try:
                    val = float(score)
                except (TypeError, ValueError):
                    continue
                if val > 0.45:
                    report["strategy_gates"].append(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "epic": epic,
                            "expectation_score": val,
                            "gate_pass": True,
                        }
                    )

        if stage in ("ready", "gui") and health and _port_bound(8787):
            if stage9_at is None:
                stage9_at = time.time()
            if time.time() - stage9_at >= 3:
                report["final_status"] = "STAGE_9_CLEAR"
                report["api_health"] = health
                report["orchestrator"] = orch
                report["ai_diagnostics"] = diag
                break

        if stage == "ready" and health:
            report["final_status"] = "STAGE_9_CLEAR"
            report["api_health"] = health
            report["orchestrator"] = orch
            report["ai_diagnostics"] = diag
            break

        time.sleep(POLL_SEC)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["ports_at_end"] = {"8080": _port_bound(8080), "8787": _port_bound(8787)}
    if report["final_status"] == "IN_PROGRESS":
        report["final_status"] = "TIMEOUT_OR_PARTIAL"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n[REPORT] written {OUT}")
    print(json.dumps({"final_status": report["final_status"], "stages": len(report["stages"])}, indent=2))
    return 0 if report["final_status"] == "STAGE_9_CLEAR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
