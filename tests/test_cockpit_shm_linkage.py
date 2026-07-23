"""Cockpit SHM linkage — zombie detection and desktop state machine."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.ipc.cockpit_shm_passive import (
    LINK_LIVE,
    LINK_NO_SEGMENT,
    LINK_STALE_SHM,
    classify_cockpit_shm,
    pid_is_alive,
    resolve_cockpit_shm_name,
    resolve_cockpit_shm_name_for_reader,
)


def _load_desktop_cockpit():
    spec = importlib.util.spec_from_file_location(
        "desktop_cockpit",
        ROOT / "scripts" / "desktop_cockpit.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CockpitShmLinkageTests(unittest.TestCase):
    def test_pid_is_alive_current_process(self) -> None:
        import os

        self.assertTrue(pid_is_alive(os.getpid()))

    def test_pid_is_alive_dead_pid(self) -> None:
        self.assertFalse(pid_is_alive(999_999_999))

    def test_classify_no_segment(self) -> None:
        state, detail = classify_cockpit_shm(None)
        self.assertEqual(state, LINK_NO_SEGMENT)
        self.assertIn("not published", detail)

    def test_classify_stale_when_pid_dead(self) -> None:
        view = {"agent_pid": 999_999_999, "ticks_cached": 43200}
        state, detail = classify_cockpit_shm(view)
        self.assertEqual(state, LINK_STALE_SHM)
        self.assertIn("do not trust", detail)

    def test_classify_live_when_pid_alive(self) -> None:
        import os

        view = {"agent_pid": os.getpid(), "ticks_cached": 100}
        state, _ = classify_cockpit_shm(view)
        self.assertEqual(state, LINK_LIVE)

    def test_dual_port_cockpit_shm_names_are_distinct(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IG_V32_DUAL_PORT": "1",
                "IG_ACCOUNT_ID": "Z6BAH4",
                "IG_COCKPIT_SHM_NAME": "",
            },
            clear=False,
        ):
            cfd = resolve_cockpit_shm_name()
        with patch.dict(
            os.environ,
            {
                "IG_V32_DUAL_PORT": "1",
                "IG_ACCOUNT_ID": "Z6BAH3",
                "IG_COCKPIT_SHM_NAME": "",
            },
            clear=False,
        ):
            sb = resolve_cockpit_shm_name()
        self.assertNotEqual(cfd, sb)
        self.assertEqual(cfd, "ig_agent_v33_cockpit_Z6BAH4")
        self.assertEqual(sb, "ig_agent_v33_cockpit_Z6BAH3")
        with patch.dict(os.environ, {"IG_V32_DUAL_PORT": "1"}, clear=False):
            reader = resolve_cockpit_shm_name_for_reader()
        self.assertEqual(reader, "ig_agent_v33_cockpit_Z6BAH4")

    def test_desktop_classify_agent_offline(self) -> None:
        mod = _load_desktop_cockpit()
        with patch.object(mod, "_manual_stop_active", return_value=False):
            link = mod._classify_linkage(None, "segment missing", {})
        self.assertEqual(link["state"], mod.STATE_AGENT_OFFLINE)
        self.assertIn("flight_deck", link["recovery"])

    def test_desktop_classify_manual_stop(self) -> None:
        mod = _load_desktop_cockpit()
        with patch.object(mod, "_manual_stop_active", return_value=True):
            link = mod._classify_linkage(None, None, {})
        self.assertEqual(link["state"], mod.STATE_MANUAL_STOP)

    def test_desktop_classify_stale_shm(self) -> None:
        mod = _load_desktop_cockpit()
        view = {
            "link_state": "STALE_SHM",
            "publisher_alive": False,
            "agent_pid": 1,
            "ticks_cached": 999,
        }
        with patch.object(mod, "_manual_stop_active", return_value=False):
            link = mod._classify_linkage(view, "stale", {})
        self.assertEqual(link["state"], mod.STATE_STALE_SHM)

    def test_desktop_classify_api_only_fallback(self) -> None:
        mod = _load_desktop_cockpit()
        health = {
            "agent_pid": 12345,
            "agent_alive": True,
            "boot_metrics": {"ready": True, "percent": 100},
        }
        with patch.object(mod, "_manual_stop_active", return_value=False):
            link = mod._classify_linkage(None, "no shm", health)
        self.assertEqual(link["state"], mod.STATE_API_ONLY)


if __name__ == "__main__":
    unittest.main()
