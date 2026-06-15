"""Release notes ↔ splash sync tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ReleaseNotesTests(unittest.TestCase):
    def test_changelog_matches_app_version(self) -> None:
        from system.app_identity import APP_VERSION
        from system.release_notes import validate_current_release_notes

        ok, msg = validate_current_release_notes()
        self.assertTrue(ok, msg)

    def test_parses_current_version_bullets(self) -> None:
        from system.app_identity import APP_VERSION
        from system.release_notes import current_release_notes

        notes = current_release_notes()
        self.assertEqual(notes["version"], APP_VERSION)
        self.assertTrue(notes["changelog_found"])
        self.assertGreaterEqual(len(notes["highlights"]), 3)
        self.assertTrue(notes["title"])

    def test_splash_api_includes_changelog(self) -> None:
        from api.dashboard_data import read_version_state

        state = read_version_state()
        self.assertIsInstance(state.get("changelog"), list)
        self.assertGreater(len(state["changelog"]), 0)
        self.assertTrue(state.get("splash_title"))


if __name__ == "__main__":
    unittest.main()
