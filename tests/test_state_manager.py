"""Tests for atomic state_manager persistence."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import system.state_manager as sm


class StateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agent_state.json"
        sm.reset_state_manager_for_tests()
        sm.set_state_path_for_tests(self.path)

    def tearDown(self) -> None:
        sm.reset_state_manager_for_tests()
        self.tmp.cleanup()

    def test_set_get_section_roundtrip(self) -> None:
        sm.set_section("points", {"state": "HEALTHY", "cumulative": 12})
        self.assertEqual(sm.get_section("points")["state"], "HEALTHY")
        sm.flush_save()
        sm.reset_state_manager_for_tests()
        sm.set_state_path_for_tests(self.path)
        self.assertTrue(sm.load_state())
        self.assertEqual(sm.get_section("points")["cumulative"], 12)

    def test_atomic_write_no_temp_leftovers(self) -> None:
        sm.set_section("demo", {"x": 1})
        sm.flush_save()
        sm.flush_save()
        leftovers = [
            p for p in self.path.parent.iterdir() if p.name.startswith(".state_")
        ]
        self.assertEqual(leftovers, [])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], sm.STATE_VERSION)
        self.assertEqual(data["sections"]["demo"]["x"], 1)

    def test_corrupt_file_uses_defaults_once(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with patch("system.state_manager.log_engine") as log_mock:
            ok1 = sm.load_state()
            ok2 = sm.load_state()
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        corrupt_logs = [c for c in log_mock.call_args_list if "corrupt" in str(c)]
        self.assertEqual(len(corrupt_logs), 1)

    def test_missing_file_silent(self) -> None:
        with patch("system.state_manager.log_engine") as log_mock:
            self.assertFalse(sm.load_state())
        self.assertEqual(log_mock.call_count, 0)

    def test_request_save_throttled(self) -> None:
        sm.set_section("a", {"n": 1})
        sm.flush_save()
        first_mtime = self.path.stat().st_mtime_ns
        sm.request_save()
        sm.request_save()
        second_mtime = self.path.stat().st_mtime_ns
        self.assertEqual(first_mtime, second_mtime)

    def test_registered_collector_save_load(self) -> None:
        store: dict[str, int] = {"v": 7}

        def dump() -> dict:
            return dict(store)

        def load(payload: dict) -> None:
            store.clear()
            store.update(payload)

        sm.register_section("runtime", dump, load)
        sm.flush_save()
        store["v"] = 0
        self.assertTrue(sm.load_state())
        self.assertEqual(store["v"], 7)

    def test_state_manager_class_custom_path(self) -> None:
        alt = Path(self.tmp.name) / "alt.json"
        mgr = sm.StateManager(alt)
        mgr.set_section("cfg", {"ok": True})
        mgr.save()
        self.assertTrue(alt.exists())
        raw = mgr.read_file()
        assert raw is not None
        self.assertTrue(raw["sections"]["cfg"]["ok"])

    def test_maybe_autosave_respects_interval(self) -> None:
        sm.set_section("t", {"i": 1})
        sm.flush_save()
        mtime1 = self.path.stat().st_mtime_ns
        sm.maybe_autosave(interval_sec=3600.0)
        mtime2 = self.path.stat().st_mtime_ns
        self.assertEqual(mtime1, mtime2)
        with patch("system.state_manager._last_autosave_ts", time.time() - 31.0):
            sm.set_section("t", {"i": 2})
            sm.maybe_autosave(interval_sec=30.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["sections"]["t"]["i"], 2)


if __name__ == "__main__":
    unittest.main()
