#!/usr/bin/env python3
"""CIAO v28 operational audit — analyze idle state, queue proposals, attempt §21 promotion."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai.operational.auto_repair import AutoRepairEngine
from ai.operational.profiler import get_operational_profiler
from ai.paths import strategy_proposals_path
from ai.staging.envelope import StagingEnvelope
from ai.strategy.backtest_simulator import (
    load_strategy_proposals,
    save_strategy_proposals,
)
from ai.strategy.performance_reviewer import (
    build_friction_matrix,
    read_quotes_from_dashboard_snapshot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def analyze_idle_state() -> dict:
    quotes = read_quotes_from_dashboard_snapshot()
    epics = list(quotes.keys())[:12]
    friction = build_friction_matrix(epics, quotes=quotes) if epics else {}
    profiler = get_operational_profiler().rolling_percentiles()
    return {
        "ts": _utc_now(),
        "epics_monitored": epics,
        "friction_matrix": friction,
        "profiler_latency": profiler,
        "idle_diagnosis": {
            "effective_confidence_floor": 80.0,
            "config_confidence_floor": 72.0,
            "note": (
                "points_engine CONF_MARGINAL_MIN=80 caps live threshold above config 72%; "
                "Japan 225 shadow samples ~59% — blocked at signal_confidence"
            ),
            "dominant_gate_block": "signal_confidence",
            "frozen_law": (
                "confidence_floor, risk_bands, probe sizing cannot auto-promote via §19/§21"
            ),
        },
    }


def queue_strategy_review(analysis: dict) -> dict:
    store = load_strategy_proposals()
    proposal = {
        "id": "idle_23h_confidence_audit",
        "name": "23h idle — confidence floor / risk envelope review",
        "epic": "IX.D.NIKKEI.IFM.IP",
        "status": "blocked_frozen_law",
        "created_at": _utc_now(),
        "analysis": analysis["idle_diagnosis"],
        "friction_matrix": analysis.get("friction_matrix"),
        "profiler_latency": analysis.get("profiler_latency"),
        "requested_config_patch": {
            "confidence_floor": 59,
            "signal_threshold": 59,
            "note": "REJECTED — requires human spec amendment + manual approval",
        },
        "recommended_operator_action": (
            "Do not auto-lower 72% floor. Review gate_coherence_snapshot.json; "
            "if intentional, edit config manually and run pre_flight_check + e2e."
        ),
    }
    store.setdefault("proposals", [])
    store["proposals"] = [
        p for p in store["proposals"] if p.get("id") != proposal["id"]
    ]
    store["proposals"].append(proposal)
    save_strategy_proposals(store)
    return proposal


def stage_blocked_risk_patch() -> str:
    """Stage a patch that MUST fail §21 frozen-key validation."""
    opt_id = "opt_002_confidence_blocked"
    envelope = StagingEnvelope()
    opt_dir = envelope.optimization_dir(opt_id)
    patch = opt_dir / "patch" / "config_v25_patch.json"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        json.dumps({"confidence_floor": 59, "signal_threshold": 59}, indent=2) + "\n",
        encoding="utf-8",
    )
    (opt_dir / "evidence.json").write_text(
        json.dumps(
            {
                "motivation": "Attempt to lower confidence for 23h idle (expected BLOCK)",
                "analysis_ref": "idle_23h_confidence_audit",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    envelope.create_manifest(
        opt_id,
        patches=[
            {
                "relative_target": "config/config_v25.json",
                "patch_file": "patch/config_v25_patch.json",
            }
        ],
        evidence={"blocked_expectation": "OperationalBoundaryError frozen keys"},
    )
    return opt_id


def main() -> int:
    analysis = analyze_idle_state()
    proposal = queue_strategy_review(analysis)
    opt_id = stage_blocked_risk_patch()
    engine = AutoRepairEngine()
    promote = engine.promote_staged_optimization(opt_id)

    print("=== CIAO OPERATIONAL AUDIT ===")
    print(json.dumps(analysis, indent=2)[:4000])
    print("\n=== STRATEGY PROPOSAL QUEUED ===")
    print(json.dumps(proposal, indent=2))
    print(f"\n=== §21 PROMOTION ATTEMPT ({opt_id}) ===")
    print(json.dumps(promote, indent=2))
    print(f"\nProposals file: {strategy_proposals_path()}")
    return 0 if not promote.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
