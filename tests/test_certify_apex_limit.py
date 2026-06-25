"""certify_apex_limit.py — dry-run matrix tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import scripts.certify_apex_limit as cert  # noqa: E402


def test_replay_profiles_count() -> None:
    assert len(cert.REPLAY_PROFILES) == 10
    assert len(cert.CERT_EPICS) == 4


def test_dry_run_certification() -> None:
    ledger = asyncio.run(
        cert.run_certification(api_base="http://127.0.0.1:9", dry_run=True, max_retries_per_cycle=1)
    )
    assert ledger.success_count() == 10
    report = ledger.to_report()
    assert report["total_injected_cycles"] == "10/10"


def test_report_serializable() -> None:
    ledger = cert.CertificationLedger(session_id="test")
    ledger.cycles.append(
        cert.CycleResult(
            cycle=1,
            epic=cert.CERT_EPICS[0],
            profile="trend_expansion",
            direction="BUY",
            ok=True,
            deal_id="D1",
        )
    )
    data = ledger.to_report()
    assert data["ok"] is False  # only 1/10
    assert data["cycles"][0]["deal_id"] == "D1"
