"""Tests for in-process v26 shadow service."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from system.v26_shadow_service import (
    _should_run,
    reset_v26_shadow_service_for_tests,
    start_v26_shadow_service,
    stop_v26_shadow_service,
)


class V26ShadowServiceTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_v26_shadow_service_for_tests()

    def test_disabled_in_pytest_by_default(self) -> None:
        with patch.dict(os.environ, {"IG_AGENT_PYTEST": "1"}, clear=False):
            with patch(
                "system.v26_shadow_service._shadow_settings",
                return_value={"enabled": True, "skip_in_pytest": True},
            ):
                self.assertFalse(_should_run())

    def test_start_noop_when_disabled(self) -> None:
        with patch("system.v26_shadow_service._should_run", return_value=False):
            start_v26_shadow_service()
        stop_v26_shadow_service()


if __name__ == "__main__":
    unittest.main()
