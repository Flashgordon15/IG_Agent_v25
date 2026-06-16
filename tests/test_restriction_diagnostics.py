"""Restriction diagnostics enrichment for dashboard standby banner."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from api.restriction_diagnostics import enrich_restrictions_payload


class RestrictionDiagnosticsTests(unittest.TestCase):
    @patch("system.config_loader.get_config")
    def test_enrich_injects_hydration_and_config(self, mock_get_config: MagicMock) -> None:
        cfg = MagicMock()
        cfg.max_open_positions = 0
        cfg.get.return_value = True
        mock_get_config.return_value = cfg

        out = enrich_restrictions_payload({"system_state": {"hydration": {}}})
        self.assertEqual(out["config"]["max_open_positions"], 0)
        self.assertTrue(out["config"]["enforce_top3_rotation_filter"])
        self.assertEqual(out["system_state"]["hydration"]["max_open_positions"], 0)


if __name__ == "__main__":
    unittest.main()
