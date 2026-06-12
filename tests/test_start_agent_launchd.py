"""Boot grace for launchd agent start."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.start_agent_launchd import boot_grace_sleep_if_needed


class StartAgentLaunchdBootGraceTests(unittest.TestCase):
    def test_skips_sleep_when_port_and_network_ready(self) -> None:
        with (
            patch(
                "scripts.start_agent_launchd.check_port_available", return_value=True
            ),
            patch(
                "scripts.start_agent_launchd.network_interfaces_ready", return_value=True
            ),
            patch("scripts.start_agent_launchd.time.sleep") as mock_sleep,
        ):
            boot_grace_sleep_if_needed(grace_sec=15.0)
        mock_sleep.assert_not_called()

    def test_sleeps_when_port_busy(self) -> None:
        with (
            patch(
                "scripts.start_agent_launchd.check_port_available", return_value=False
            ),
            patch(
                "scripts.start_agent_launchd.network_interfaces_ready", return_value=True
            ),
            patch("scripts.start_agent_launchd.time.sleep") as mock_sleep,
        ):
            boot_grace_sleep_if_needed(grace_sec=15.0)
        mock_sleep.assert_called_once_with(15.0)


if __name__ == "__main__":
    unittest.main()
