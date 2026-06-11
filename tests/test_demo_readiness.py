"""Tests for DEMO readiness helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.types import ExecutionMode
from system.demo_readiness import (
    ReadinessCheckResult,
    check_demo_credentials_ready,
    check_demo_execution_ready,
    check_rate_limit_ready,
)


class DemoReadinessTests(unittest.TestCase):
    def test_rate_limit_ready_when_inactive(self) -> None:
        with patch("system.rate_limit_manager.get_rate_limit_manager") as mock_mgr:
            mock_mgr.return_value.is_active.return_value = False
            result = check_rate_limit_ready()
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "rate_limit")

    @patch("system.demo_readiness.credentials_path")
    @patch("system.demo_readiness.try_load_credentials")
    def test_credentials_missing_file(
        self, mock_try: MagicMock, mock_path: MagicMock
    ) -> None:
        missing = MagicMock()
        missing.is_file.return_value = False
        mock_path.return_value = missing
        result = check_demo_credentials_ready()
        self.assertFalse(result.ok)
        self.assertEqual(result.name, "credentials")

    def test_execution_ready_with_bot(self) -> None:
        mock_engine = MagicMock()
        mock_engine.mode = ExecutionMode.DEMO
        mock_engine._live = object()
        mock_loop = MagicMock()
        mock_loop.execution_engine = mock_engine
        mock_loop.signal_engine = MagicMock()
        bot = MagicMock()
        with patch(
            "system.demo_readiness._execution_loop_from_bot", return_value=mock_loop
        ):
            result = check_demo_execution_ready(bot=bot)
        self.assertTrue(result.ok)

    def test_readiness_check_result_dataclass(self) -> None:
        r = ReadinessCheckResult("x", True, "ok", {"a": 1})
        self.assertTrue(r.ok)
        self.assertEqual(r.details["a"], 1)


if __name__ == "__main__":
    unittest.main()
