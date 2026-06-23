"""Cockpit feed guardian — stall detection and heal decisions."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.cockpit_feed_guardian import (
    HEAL_FEED_RESET,
    HEAL_START_AGENT,
    HEAL_USE_API,
    FeedWatchState,
    decide_heal_action,
    is_publish_stalled,
    pid_mismatch,
    update_feed_watch,
)


class CockpitFeedGuardianTests(unittest.TestCase):
    def test_pid_mismatch_detected(self) -> None:
        view = {"agent_pid": 100}
        health = {"agent_pid": 200, "agent_alive": True}
        self.assertTrue(pid_mismatch(view, health))

    def test_write_seq_stall(self) -> None:
        watch = FeedWatchState()
        view = {"write_seq": 5, "ticks_cached": 100, "live_ram_ticks": 10}
        update_feed_watch(watch, view)
        t0 = time.monotonic()
        stalled, frozen, reason = is_publish_stalled(watch, view, now=t0 + 4.0)
        self.assertTrue(stalled)
        self.assertEqual(reason, "write_seq_stalled")
        self.assertGreaterEqual(frozen, 3.0)

    def test_quiet_market_not_stalled_when_seq_advances(self) -> None:
        watch = FeedWatchState()
        v1 = {"write_seq": 1, "ticks_cached": 100, "live_ram_ticks": 10}
        update_feed_watch(watch, v1)
        t1 = time.monotonic()
        v2 = {"write_seq": 2, "ticks_cached": 100, "live_ram_ticks": 10}
        update_feed_watch(watch, v2, now=t1 + 0.5)
        stalled, _, _ = is_publish_stalled(watch, v2, now=t1 + 1.0)
        self.assertFalse(stalled)

    def test_stale_shm_triggers_api_heal(self) -> None:
        watch = FeedWatchState()
        action, _ = decide_heal_action(
            link_state="STALE_SHM",
            stalled=True,
            stall_reason="write_seq_stalled",
            health={"agent_pid": 1, "agent_alive": True, "boot_metrics": {"ready": True}},
            view={"agent_pid": 99},
            watch=watch,
        )
        self.assertEqual(action, HEAL_USE_API)

    def test_offline_triggers_agent_start(self) -> None:
        watch = FeedWatchState()
        action, _ = decide_heal_action(
            link_state="AGENT_OFFLINE",
            stalled=True,
            stall_reason="write_seq_stalled",
            health={},
            view=None,
            watch=watch,
        )
        self.assertEqual(action, HEAL_START_AGENT)

    def test_live_stall_triggers_feed_reset(self) -> None:
        watch = FeedWatchState()
        watch.last_heal_mono = 0.0
        action, _ = decide_heal_action(
            link_state="LIVE",
            stalled=True,
            stall_reason="write_seq_stalled",
            health={"agent_pid": 1, "agent_alive": True, "boot_metrics": {"ready": True}},
            view={"agent_pid": 1, "write_seq": 3},
            watch=watch,
            now=100.0,
        )
        self.assertEqual(action, HEAL_FEED_RESET)


if __name__ == "__main__":
    unittest.main()
