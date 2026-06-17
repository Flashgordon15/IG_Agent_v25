"""Unit tests — project-root .env credential loader."""

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
from system.env_loader import (
    apply_env_to_credentials,
    env_credentials_complete,
    load_dotenv,
)


class EnvLoaderTests(unittest.TestCase):
    def test_env_overlay_maps_canonical_fields(self) -> None:
        env = {
            "IG_USERNAME": "user1",
            "IG_PASSWORD": "pass1",
            "IG_API_KEY": "key1",
            "ACCOUNT_TYPE": "DEMO",
            "IG_ACCOUNT_ID": "Z6BAH4",
        }
        with patch.dict(os.environ, env, clear=True):
            raw = apply_env_to_credentials({})
            complete = env_credentials_complete()
        self.assertEqual(raw["ig_username"], "user1")
        self.assertEqual(raw["ig_password"], "pass1")
        self.assertEqual(raw["ig_api_key"], "key1")
        self.assertEqual(raw["ig_account_type"], "DEMO")
        self.assertEqual(raw["ig_account_id"], "Z6BAH4")
        self.assertTrue(complete)

    def test_dotenv_file_loads_into_environ(self) -> None:
        from system.env_loader import reset_dotenv_state

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "IG_USERNAME=from_file\nIG_PASSWORD=secret\n"
                "IG_API_KEY=api\nACCOUNT_TYPE=DEMO\nIG_ACCOUNT_ID=Z6BAH4\n",
                encoding="utf-8",
            )
            with patch("system.env_loader.dotenv_path", return_value=env_path):
                with patch.dict(os.environ, {}, clear=True):
                    reset_dotenv_state()
                    load_dotenv(override=True)
                    self.assertEqual(os.environ.get("IG_USERNAME"), "from_file")
                    self.assertEqual(os.environ.get("ACCOUNT_TYPE"), "DEMO")

    def test_credentials_from_env_only_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cred_path = Path(tmp) / "missing.json"
            with patch.dict(
                os.environ,
                {
                    "IG_USERNAME": "u",
                    "IG_PASSWORD": "p",
                    "IG_API_KEY": "k",
                    "ACCOUNT_TYPE": "DEMO",
                    "IG_ACCOUNT_ID": "Z6BAH4",
                },
                clear=False,
            ):
                status = try_load_credentials(path=cred_path)
        self.assertTrue(status.ok)
        assert status.credentials is not None
        self.assertEqual(status.credentials.ig_username, "u")


if __name__ == "__main__":
    unittest.main()
