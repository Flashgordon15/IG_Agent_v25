"""Flight Deck cockpit integrity — SRE API surfaces and static asset contracts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COCKPIT_APP_JS = REPO_ROOT / "cockpit-web" / "app.js"


@pytest.fixture(autouse=True)
def _seed_iron_ledger():
    from runtime import master_orchestrator as mo
    from system import chaos_guardian as cg

    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()


def test_cockpit_app_js_performance_contracts():
    text = COCKPIT_APP_JS.read_text(encoding="utf-8")
    assert "async function fetchJson" in text
    assert re.search(r"const LOG_MAX_LINES = 25", text)
    assert re.search(r"const TRIAGE_MAX_LINES = 25", text)
    assert "document.createDocumentFragment()" in text
    assert "startSrePollLoop" in text
    assert "fetchSreBundle" in text
    assert "Promise.allSettled" in text
    assert "/api/orchestrator_state" in text
    assert "/api/guardian_status" in text
    assert "/api/reporting_status" in text
    assert "/api/macro_steering" in text


def test_macro_steering_snapshot_shape():
    from cockpit.sre_snapshots import get_macro_steering_snapshot

    snap = get_macro_steering_snapshot("CS.D.EURUSD.CFD.IP")
    assert snap.get("ok") is True
    assert snap.get("epic") == "CS.D.EURUSD.CFD.IP"
    assert isinstance(snap.get("macro"), dict)
    assert isinstance(snap.get("sentiment"), dict)
    assert isinstance(snap.get("news"), dict)
    assert isinstance(snap.get("shadow_walk"), dict)
    assert "delta_5m" in snap["sentiment"]
    assert "delta_30m" in snap["sentiment"]
    assert "seconds_to_next" in snap["news"]


def test_macro_steering_default_epic_is_gold():
    from cockpit.sre_snapshots import get_macro_steering_snapshot

    snap = get_macro_steering_snapshot()
    assert snap.get("epic") == "CS.D.CFPGOLD.CFP.IP"


def test_orchestrator_state_scoreboard_fields():
    from runtime.master_orchestrator import get_orchestrator_state_snapshot

    snap = get_orchestrator_state_snapshot()
    sb = snap.get("scoreboard") or {}
    assert "total_pp" in sb
    assert "rank" in sb
    assert "capacity_multiplier" in sb
    assert "size_factor_multiplier" in sb


def test_guardian_status_reconciliation_fields():
    from system.chaos_guardian import get_guardian_status_snapshot

    snap = get_guardian_status_snapshot()
    assert "reconciliation_registers" in snap
    assert "state_sync_discrepancies" in snap
    assert "token_buckets" in snap
    registers = snap["reconciliation_registers"]
    assert isinstance(registers, dict)
    assert "registers" in registers


def test_reporting_status_fields():
    from system.alert_reporting_matrix import get_reporting_status_snapshot

    snap = get_reporting_status_snapshot()
    assert "healthy" in snap
    assert "queue_depth" in snap
    assert "subsystem_status" in snap


def test_cockpit_web_server_exposes_sre_routes():
    from cockpit.web_server import create_cockpit_app

    app = create_cockpit_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    for required in (
        "/api/orchestrator_state",
        "/api/guardian_status",
        "/api/reporting_status",
        "/api/macro_steering",
        "/api/cockpit_ready",
    ):
        assert required in paths


def test_cockpit_ready_snapshot_shape():
    from cockpit.web_server import build_cockpit_ready_snapshot

    snap = build_cockpit_ready_snapshot()
    assert isinstance(snap.get("checks"), dict)
    assert "checks_passed" in snap
    assert snap.get("checks_total", 0) >= 3


@pytest.mark.parametrize("iteration", range(24))
def test_sre_snapshot_flood_resilience(iteration: int):
    """Rapid snapshot refresh must not raise under simulated poll flood."""
    from cockpit.sre_snapshots import get_macro_steering_snapshot
    from runtime.master_orchestrator import get_orchestrator_state_snapshot
    from system.alert_reporting_matrix import get_reporting_status_snapshot
    from system.chaos_guardian import get_guardian_status_snapshot

    _ = get_orchestrator_state_snapshot()
    _ = get_guardian_status_snapshot()
    _ = get_reporting_status_snapshot()
    _ = get_macro_steering_snapshot()
