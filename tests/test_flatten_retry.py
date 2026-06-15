"""Tests for session-end flatten retry coordinator (Fix F)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from system.config import Config
from trading.flatten_retry import (
    flatten_backoff_seconds,
    flatten_max_retries,
    flatten_slow_monitor_interval,
    get_flatten_retry_state,
    on_flatten_confirmed,
    on_flatten_verify_failed,
    reset_flatten_retry_state,
    should_run_flatten_retry,
)


def _cfg() -> Config:
    return Config(
        _data={
            "flatten_retry": {
                "flatten_max_retries": 5,
                "flatten_retry_backoff_seconds": [30, 60, 120, 240, 480],
                "flatten_slow_monitor_interval_seconds": 600,
            }
        }
    )


class FlattenRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_flatten_retry_state()

    def test_backoff_intervals_correct(self) -> None:
        cfg = _cfg()
        self.assertEqual(flatten_max_retries(cfg), 5)
        self.assertEqual(
            flatten_backoff_seconds(cfg), [30, 60, 120, 240, 480]
        )
        self.assertEqual(flatten_slow_monitor_interval(cfg), 600.0)

    def test_flatten_retry_exhausts_after_max_attempts(self) -> None:
        cfg = _cfg()
        t0 = 1_000_000.0
        notify = MagicMock()
        for i in range(5):
            st = on_flatten_verify_failed(
                "CS.D.CFPGOLD.CFP.IP",
                1,
                cfg=cfg,
                now=t0 + i,
                notify=notify,
            )
        self.assertEqual(st.retry_count, 5)
        self.assertTrue(st.abandoned)
        self.assertTrue(st.slow_monitor_active)
        notify.assert_called()

    def test_flatten_slow_monitor_activates_after_abandonment(self) -> None:
        cfg = _cfg()
        t0 = 2_000_000.0
        for _ in range(5):
            on_flatten_verify_failed("GOLD", 1, cfg=cfg, now=t0)
        st = get_flatten_retry_state()
        self.assertTrue(st.slow_monitor_active)
        self.assertIsNotNone(st.next_attempt_at)
        self.assertFalse(should_run_flatten_retry(cfg=cfg, now=t0 + 30))
        self.assertTrue(should_run_flatten_retry(cfg=cfg, now=t0 + 600))

    def test_confirmed_clears_state(self) -> None:
        on_flatten_verify_failed("GOLD", 1, cfg=_cfg(), now=100.0)
        on_flatten_confirmed()
        st = get_flatten_retry_state()
        self.assertEqual(st.retry_count, 0)
        self.assertFalse(st.slow_monitor_active)


if __name__ == "__main__":
    unittest.main()
