"""Friday hard flatten protocol tests."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from system.config import Config
from trading.friday_flatten import (
    friday_flatten_snapshot,
    reset_friday_flatten_state,
    run_friday_flatten_tick,
)

_LONDON = ZoneInfo("Europe/London")
GOLD = "CS.D.CFPGOLD.CFP.IP"


def _cfg(**overrides) -> Config:
    data = {
        "friday_flatten": {
            "enabled": True,
            "friday_flatten_time_bst": "19:30",
            "friday_flatten_confirm_time_bst": "19:45",
            "friday_flatten_override": False,
        }
    }
    data.update(overrides)
    return Config(_data=data)


def _friday_at(h: int, m: int) -> datetime:
    return datetime(2026, 6, 19, h, m, tzinfo=_LONDON)


class FridayFlattenTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_friday_flatten_state()

    def test_friday_flatten_triggers_at_1930_bst(self) -> None:
        cfg = _cfg()
        closed: list[str] = []
        at = _friday_at(19, 30)

        def _close() -> int:
            closed.append("close")
            return 1

        run_friday_flatten_tick(
            cfg=cfg,
            now=at,
            execute_close=_close,
            verify_close=lambda _a: None,
            open_count_fn=lambda: 1,
            list_positions_fn=lambda: [{"epic": GOLD, "entry": 100}],
            mono_now=1000.0,
        )
        self.assertEqual(closed, ["close"])
        snap = friday_flatten_snapshot(cfg)
        self.assertTrue(snap["active"])

    def test_friday_flatten_suspended_by_override(self) -> None:
        cfg = _cfg(friday_flatten={"enabled": True, "friday_flatten_override": True})
        called = False

        def _close() -> int:
            nonlocal called
            called = True
            return 0

        run_friday_flatten_tick(
            cfg=cfg,
            now=_friday_at(19, 30),
            execute_close=_close,
            verify_close=lambda _a: None,
            open_count_fn=lambda: 2,
            mono_now=2000.0,
        )
        self.assertFalse(called)

    def test_friday_flatten_confirms_flat_at_1945(self) -> None:
        cfg = _cfg()
        logs: list[str] = []

        with patch("trading.friday_flatten.log_engine", side_effect=logs.append):
            run_friday_flatten_tick(
                cfg=cfg,
                now=_friday_at(19, 45),
                execute_close=lambda: 0,
                verify_close=lambda _a: None,
                open_count_fn=lambda: 0,
                mono_now=3000.0,
            )
        self.assertTrue(any("Confirmed flat" in m for m in logs))

    def test_friday_flatten_alerts_if_not_flat(self) -> None:
        cfg = _cfg()
        messages: list[str] = []
        run_friday_flatten_tick(
            cfg=cfg,
            now=_friday_at(19, 30),
            execute_close=lambda: 1,
            verify_close=lambda _a: None,
            open_count_fn=lambda: 1,
            mono_now=4000.0,
        )
        run_friday_flatten_tick(
            cfg=cfg,
            now=_friday_at(19, 45),
            execute_close=lambda: 0,
            verify_close=lambda _a: None,
            open_count_fn=lambda: 1,
            list_positions_fn=lambda: [
                {"epic": GOLD, "side": "BUY", "ig_deal_id": "D1"}
            ],
            notify=messages.append,
            mono_now=5000.0,
        )
        self.assertTrue(any("FLATTEN ALERT" in m for m in messages))

    def test_friday_flatten_uses_existing_close_infrastructure(self) -> None:
        cfg = _cfg()
        execute = MagicMock(return_value=2)
        verify = MagicMock()
        run_friday_flatten_tick(
            cfg=cfg,
            now=_friday_at(19, 31),
            execute_close=execute,
            verify_close=verify,
            open_count_fn=lambda: 2,
            mono_now=6000.0,
        )
        execute.assert_called_once()
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
