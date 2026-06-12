"""Demo-only deployment guard tests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.demo_guard import demo_only_enforced, validate_demo_only_startup


class DemoGuardTests(unittest.TestCase):
    def test_blocks_live_operating_mode(self) -> None:
        cfg = {
            "demo_only_deployment": True,
            "allow_live_trading": False,
            "operating_mode": "LIVE",
        }
        ok, msg = validate_demo_only_startup(cfg)
        self.assertFalse(ok)
        self.assertIn("LIVE", msg)

    def test_blocks_allow_live_trading(self) -> None:
        cfg = {
            "demo_only_deployment": True,
            "allow_live_trading": True,
            "operating_mode": "DEMO",
        }
        ok, msg = validate_demo_only_startup(cfg)
        self.assertFalse(ok)
        self.assertIn("allow_live_trading", msg)

    def test_passes_demo_config(self) -> None:
        cfg = {
            "demo_only_deployment": True,
            "allow_live_trading": False,
            "operating_mode": "DEMO",
        }
        ok, msg = validate_demo_only_startup(cfg)
        self.assertTrue(ok)

    @patch.dict(os.environ, {"IG_AGENT_ALLOW_LIVE": "1"}, clear=False)
    def test_override_env_disables_enforcement(self) -> None:
        cfg = {
            "demo_only_deployment": True,
            "allow_live_trading": True,
            "operating_mode": "LIVE",
        }
        self.assertFalse(demo_only_enforced(cfg))


if __name__ == "__main__":
    unittest.main()
