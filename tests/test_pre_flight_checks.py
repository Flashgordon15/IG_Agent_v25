"""Tests for pre-flight operational checks."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.gate_activity import record_gate_evaluation, reset_gate_activity_for_tests
from system.pre_flight_checks import (
    check_anti_mock_session_summaries,
    check_gate_evaluation_recent,
    check_session_summary_integrity,
    check_startup_stream_gate_log,
)


class PreFlightChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)

    def tearDown(self) -> None:
        reset_gate_activity_for_tests()
        self.tmp.cleanup()

    def test_anti_mock_detects_magicmock(self) -> None:
        path = self.logs / "session_summary_20260602.txt"
        path.write_text(
            "Final state: <MagicMock name='mock.snapshot().nominal_state'>\n",
            encoding="utf-8",
        )
        result = check_anti_mock_session_summaries(self.logs)
        self.assertFalse(result.passed)
        self.assertIn("20260602", result.reason)

    def test_summary_integrity_accepts_valid_file(self) -> None:
        path = self.logs / "session_summary_20260527.txt"
        path.write_text(
            "IG Agent v25 — Session Summary\n"
            "Trades:      0 (0W / 0L)\n"
            "Final state: HEALTHY\n"
            "Stream uptime: 0.0%\n",
            encoding="utf-8",
        )
        result = check_session_summary_integrity(self.logs)
        self.assertTrue(result.passed)

    def test_gate_activity_recent_when_recorded(self) -> None:
        reset_gate_activity_for_tests()
        record_gate_evaluation()
        result = check_gate_evaluation_recent(max_age_sec=60.0)
        self.assertTrue(result.passed)

    def test_stream_gate_passes_for_rest_poll_in_pytest(self) -> None:
        import os

        prev = os.environ.get("IG_AGENT_PYTEST")
        os.environ["IG_AGENT_PYTEST"] = "1"
        try:
            with mock.patch(
                "system.pre_flight_checks._rest_poll_transport_active",
                return_value=True,
            ):
                result = check_startup_stream_gate_log()
            self.assertTrue(result.passed)
            self.assertIn("rest_poll", result.reason.lower())
        finally:
            if prev is None:
                os.environ.pop("IG_AGENT_PYTEST", None)
            else:
                os.environ["IG_AGENT_PYTEST"] = prev


if __name__ == "__main__":
    unittest.main()
