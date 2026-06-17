"""Unit tests — headless .env credential bridge (no interactive prompts)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.credentials_loader import try_load_credentials
from system.launch_auth import (
    apply_launch_password_to_credentials,
    prepare_launch_auth,
    resolve_launch_password,
)


class LaunchAuthTests(unittest.TestCase):
    def test_env_password_applied(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IG_PASSWORD": "headless-secret",
                "IG_USERNAME": "user",
            },
            clear=False,
        ):
            raw = apply_launch_password_to_credentials({"ig_username": "user"})
        self.assertEqual(raw["ig_password"], "headless-secret")

    def test_env_password_overrides_plaintext_json(self) -> None:
        with patch.dict(
            os.environ,
            {"IG_PASSWORD": "other"},
            clear=False,
        ):
            raw = apply_launch_password_to_credentials(
                {"ig_password": "existing", "ig_username": "user"}
            )
        self.assertEqual(raw["ig_password"], "other")

    def test_enc_prefix_triggers_env_injection(self) -> None:
        with patch.dict(
            os.environ,
            {"IG_PASSWORD": "decrypted"},
            clear=False,
        ):
            raw = apply_launch_password_to_credentials({"ig_password": "enc:opaque"})
        self.assertEqual(raw["ig_password"], "decrypted")

    def test_resolve_never_prompts(self) -> None:
        with patch.dict(os.environ, {"IG_PASSWORD": ""}, clear=False):
            with patch("getpass.getpass") as gp:
                resolved = resolve_launch_password()
        self.assertIsNone(resolved)
        gp.assert_not_called()

    def test_try_load_credentials_from_env(self) -> None:
        payload = (
            '{"ig_username":"","ig_api_key":"","ig_account_id":"",'
            '"ig_account_type":"","ig_password":""}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "credentials.json"
            cred_path.write_text(payload, encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "IG_USERNAME": "u",
                    "IG_PASSWORD": "from-env",
                    "IG_API_KEY": "k",
                    "ACCOUNT_TYPE": "DEMO",
                    "IG_ACCOUNT_ID": "Z6BAH4",
                },
                clear=False,
            ):
                status = try_load_credentials(path=cred_path)
        self.assertTrue(status.ok)
        assert status.credentials is not None
        self.assertEqual(status.credentials.ig_password, "from-env")

    def test_prepare_launch_auth_sets_admin_password(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IG_AGENT_PYTEST": "0",
                "IG_PASSWORD": "bridge-pass",
            },
            clear=False,
        ):
            os.environ.pop("ADMIN_PASSWORD", None)
            prepare_launch_auth()
            self.assertEqual(os.environ.get("ADMIN_PASSWORD"), "bridge-pass")


if __name__ == "__main__":
    unittest.main()
