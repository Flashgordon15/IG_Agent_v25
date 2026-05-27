"""Tests for splash screen behaviour — Section 4.5 Step 14."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.dashboard_data import dismiss_splash, read_version_state


class SplashTests(unittest.TestCase):
    def test_missing_file_defaults_to_show(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.json"
            with patch("api.dashboard_data.version_json_path", return_value=path):
                state = read_version_state()
            self.assertFalse(state["shown"])
            self.assertEqual(state["version"], "25.1.0")

    def test_shown_true_in_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.json"
            path.write_text(
                json.dumps({"version": "25.1.0", "shown": True}),
                encoding="utf-8",
            )
            with patch("api.dashboard_data.version_json_path", return_value=path):
                state = read_version_state()
            self.assertTrue(state["shown"])

    def test_dismiss_sets_shown_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.json"
            path.write_text(
                json.dumps({"version": "25.1.0", "shown": False}),
                encoding="utf-8",
            )
            with patch("api.dashboard_data.version_json_path", return_value=path):
                result = dismiss_splash()
                self.assertTrue(result["shown"])
                on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(on_disk["shown"])


if __name__ == "__main__":
    unittest.main()
