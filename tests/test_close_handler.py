"""Tests for manual dashboard close handler."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.close_handler import close_deal, reset_close_handler_for_tests
from system.credentials_loader import Credentials, CredentialsStatus


class CloseHandlerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_close_handler_for_tests()

    @patch("system.ig_rest_session.ensure_shared_authenticated")
    @patch("system.credentials_loader.try_load_credentials")
    @patch("system.config_loader.ConfigLoader")
    def test_default_close_uses_credentials_object(
        self,
        mock_loader: MagicMock,
        mock_try_load: MagicMock,
        mock_auth: MagicMock,
    ) -> None:
        creds = Credentials(
            ig_api_key="key",
            ig_username="user",
            ig_password="pass",
            ig_account_type="DEMO",
            ig_account_id="ACC1",
        )
        mock_try_load.return_value = CredentialsStatus(ok=True, credentials=creds)
        mock_loader.return_value.load_config.return_value.currency_code = "GBP"

        rest = MagicMock()
        rest.open_positions.return_value = [
            {
                "market": {"epic": "CS.D.CFPGOLD.CFP.IP"},
                "position": {
                    "dealId": "DIAAAAXNM2VYUAN",
                    "direction": "SELL",
                    "size": 10.0,
                },
            }
        ]
        rest.close_position.return_value = {"verified_closed": True}
        mock_auth.return_value = rest

        result = close_deal("DIAAAAXNM2VYUAN")

        mock_auth.assert_called_once_with(creds)
        rest.close_position.assert_called_once()
        self.assertTrue(result["verified_closed"])


if __name__ == "__main__":
    unittest.main()
