"""Flight Deck UI fault tolerance — corrupt payloads, null defense, recovery grid."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COCKPIT_APP_JS = REPO_ROOT / "cockpit-web" / "app.js"


@pytest.fixture
def corrupt_payloads():
    return [
        None,
        "",
        [],
        42,
        {"portfolio_synthesis": None},
        {"pp_trajectory_7d": "not-an-object"},
        {"scoreboard": {"total_pp": "NaN"}},
        {"stage_tokens": {"STAGE_1_CONFIG_SANITY": None}},
        {"news_alpha": {"headlines": {"recent": "string-not-array"}}},
        {"covariance": {"pair_cells": "invalid"}},
    ]


def test_ui_baseline_defaults_contract():
    text = COCKPIT_APP_JS.read_text(encoding="utf-8")
    assert "UI_BASELINE_DEFAULTS" in text
    assert "performance_points: 1000" in text
    assert "runFlightDeckSafe" in text
    assert "_paintTradingHubTelemetry" in text
    assert "applyAutonomicStageRecoverySweep" in text
    assert "window.__flightDeck" in text
    assert re.search(r"function safeObject\(", text)
    assert "startHeaderClockTicker()" in text


def test_poll_sre_telemetry_isolated_safe_blocks():
    text = COCKPIT_APP_JS.read_text(encoding="utf-8")
    assert "runFlightDeckSafe(\"pollSreTelemetry.frame\"" in text
    assert "runFlightDeckSafe(\"tradingHub\"" in text
    assert "runFlightDeckSafe(\"autonomicSweep\"" in text
    assert "scheduleTradingHubTelemetryRender(diag || {}, orch || {}" in text


def test_sanitizer_empty_and_corrupt_payloads(corrupt_payloads):
    from cockpit.ui_payload_sanitizer import (
        UI_BASELINE_PP,
        baseline_ui_snapshot,
        sanitize_ai_diagnostics_for_ui,
        sanitize_orchestrator_for_ui,
        sanitize_pp_trajectory,
        sanitize_recovery_payload,
    )

    for raw in corrupt_payloads:
        orch = sanitize_orchestrator_for_ui(raw)
        diag = sanitize_ai_diagnostics_for_ui(raw)
        traj = sanitize_pp_trajectory(raw)
        recovery = sanitize_recovery_payload(orch=raw, diag=raw, iron=raw)
        assert isinstance(orch, dict)
        assert isinstance(diag, dict)
        assert isinstance(traj, dict)
        assert isinstance(recovery, dict)
        assert orch["scoreboard"]["total_pp"] == UI_BASELINE_PP
        assert traj["trend"] in ("expansion", "defense", "neutral")

    baseline = baseline_ui_snapshot()
    assert baseline["platform_pp"] == UI_BASELINE_PP
    assert baseline["exposure_gbp"] == 0.0


def test_sanitizer_pp_trajectory_malformed_scores():
    from cockpit.ui_payload_sanitizer import sanitize_pp_trajectory

    out = sanitize_pp_trajectory({"pp_scores": [None, "bad", 1100], "trend": "bogus"})
    assert out["pp_scores"] == [1000, 1000, 1100]
    assert out["trend"] == "neutral"


def test_nine_stage_orchestrator_segments():
    from cockpit.desktop_splash_assets import ORCHESTRATOR_STAGES, orchestrator_segment_states

    assert len(ORCHESTRATOR_STAGES) == 9
    states = orchestrator_segment_states(
        {
            "STAGE_1_CONFIG_SANITY": "SUCCESS",
            "STAGE_5_LAUNCH_CORE": "WARMING_HEALTHY",
            "STAGE_9_ALPHAS_ARMED": "SUCCESS",
        }
    )
    assert len(states) == 9
    assert states[0] == "active"
    assert states[4] == "warming"
    assert states[8] == "active"


def test_desktop_shell_pushes_recovery_without_raise():
    from cockpit.desktop_app_shell import _push_autonomic_recovery_to_cockpit, _push_boot_stage_matrix

    calls: list[str] = []
    with patch("cockpit.desktop_app_shell._evaluate", side_effect=lambda js: calls.append(js)):
        _push_boot_stage_matrix(None, {"STAGE_1_CONFIG_SANITY": "SUCCESS"})
        _push_autonomic_recovery_to_cockpit(
            {"stage_tokens": {"STAGE_1_CONFIG_SANITY": "SUCCESS"}},
            {"synthetic_hydration_active": True, "fallback_transport_tier": "rest_poll"},
            {},
        )
    assert any("__flightDeck" in c and "applyStageTokens" in c for c in calls)
    assert any("__flightDeck" in c and "applyAutonomicRecovery" in c for c in calls)


def test_recovery_payload_json_serializable(corrupt_payloads):
    from cockpit.ui_payload_sanitizer import sanitize_recovery_payload

    for raw in corrupt_payloads:
        payload = sanitize_recovery_payload(orch=raw, diag=raw, iron=raw)
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        assert isinstance(decoded, dict)
        assert "orchestrator" in decoded
        assert "diagnostics" in decoded
