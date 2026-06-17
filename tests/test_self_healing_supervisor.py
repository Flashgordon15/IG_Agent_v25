"""Unit tests — autonomous patch_crash_* deployment supervisor."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system import self_healing_supervisor as shs
from system.self_healing_supervisor import (
    GateSuiteResult,
    SelfHealingSupervisor,
    deployment_frozen,
)


class SelfHealingSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        shs._frozen = False
        shs._last_result = {}
    def test_gate_failure_freezes_deployment(self) -> None:
        supervisor = SelfHealingSupervisor()
        with patch.object(
            supervisor,
            "run_gate_suite",
            return_value=GateSuiteResult(ok=False, failed=1, output_tail="1 failed"),
        ):
            with patch.object(supervisor, "merge_branch_to_main") as merge:
                result = supervisor.authorize_patch_deployment("patch_crash_demo")
        self.assertFalse(result["ok"])
        self.assertTrue(result["frozen"])
        merge.assert_not_called()
        self.assertTrue(deployment_frozen())

    def test_gate_pass_merges_and_hot_reloads(self) -> None:
        supervisor = SelfHealingSupervisor()
        with patch.object(
            supervisor,
            "run_gate_suite",
            return_value=GateSuiteResult(ok=True, passed=67, total=67),
        ):
            with patch.object(supervisor, "merge_branch_to_main", return_value=True):
                with patch.object(
                    supervisor,
                    "cleanup_service_ports",
                    return_value={"killed_8080": [], "killed_8787": []},
                ):
                    with patch.object(supervisor, "hot_reload_agent", return_value=True):
                        result = supervisor.authorize_patch_deployment(
                            "patch_crash_ok"
                        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["frozen"])
        self.assertTrue(result["hot_reload"])

    def test_discover_patch_branches(self) -> None:
        supervisor = SelfHealingSupervisor()
        mock_result = MagicMock()
        mock_result.stdout = "patch_crash_alpha\npatch_crash_beta\n"
        with patch("subprocess.run", return_value=mock_result):
            branches = supervisor.discover_patch_branches()
        self.assertEqual(branches, ["patch_crash_alpha", "patch_crash_beta"])


if __name__ == "__main__":
    unittest.main()
