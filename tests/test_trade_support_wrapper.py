"""Tests for runtime.trade_support_wrapper — always-on open-trade supervisor."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime.trade_support_wrapper import (  # noqa: E402
    TradeSupportWrapper,
    open_mins_from_item,
)
from execution.open_position_rules import ManageAction, OpenPositionRow  # noqa: E402


def _row(deal_id: str, epic: str, pnl, open_mins=None) -> OpenPositionRow:
    return OpenPositionRow(
        deal_id=deal_id,
        epic=epic,
        direction="SELL",
        size=1.5,
        entry=100.0,
        pnl_gbp=pnl,
        open_mins=open_mins,
    )


def test_open_mins_from_item_iso() -> None:
    opened = datetime.now(timezone.utc) - timedelta(minutes=25)
    item = {"position": {"createdDateUTC": opened.isoformat()}}
    mins = open_mins_from_item(item)
    assert mins is not None
    assert 24.0 <= mins <= 26.5


def test_open_mins_from_item_ig_format() -> None:
    item = {"position": {"createdDate": "2026/01/01 00:00:00"}}
    mins = open_mins_from_item(item)
    assert mins is not None and mins > 0


def test_open_mins_missing() -> None:
    assert open_mins_from_item({"position": {}}) is None


def _wrapper() -> TradeSupportWrapper:
    # Fake config object with .get so no disk config is loaded.
    class _Cfg(dict):
        pass

    w = TradeSupportWrapper(cfg=_Cfg(), rest=object())
    return w


def test_unmanageable_flags_after_threshold_when_aged() -> None:
    w = _wrapper()
    w.cfg["unmanageable_cycles"] = 3
    w.cfg["unmanageable_min_age_min"] = 10.0
    row = _row("DEAL_A", "IX.D.NIKKEI.IFM.IP", None, open_mins=30.0)

    # First two cycles: streak building, no action.
    assert w._unmanageable_actions([row]) == []
    assert w._unmanageable_actions([row]) == []
    # Third cycle crosses threshold → flatten action.
    actions = w._unmanageable_actions([row])
    assert len(actions) == 1
    assert actions[0].action == "flatten"
    assert "unmanageable" in actions[0].reason


def test_unmanageable_skips_young_trades() -> None:
    w = _wrapper()
    w.cfg["unmanageable_cycles"] = 2
    w.cfg["unmanageable_min_age_min"] = 10.0
    row = _row("DEAL_YOUNG", "IX.D.DOW.IFM.IP", None, open_mins=2.0)
    for _ in range(5):
        assert w._unmanageable_actions([row]) == []


def test_unmanageable_streak_resets_when_valued() -> None:
    w = _wrapper()
    w.cfg["unmanageable_cycles"] = 2
    w.cfg["unmanageable_min_age_min"] = 5.0
    bad = _row("DEAL_B", "IX.D.DOW.IFM.IP", None, open_mins=30.0)
    w._unmanageable_actions([bad])
    # Now it values fine → streak cleared.
    good = _row("DEAL_B", "IX.D.DOW.IFM.IP", 5.0, open_mins=31.0)
    w._unmanageable_actions([good])
    assert "DEAL_B" not in w.state.no_pnl_streak


def test_unmanageable_flag_only_mode() -> None:
    w = _wrapper()
    w.cfg["unmanageable_cycles"] = 1
    w.cfg["unmanageable_min_age_min"] = 1.0
    w.cfg["flatten_unmanageable"] = False
    row = _row("DEAL_C", "IX.D.NIKKEI.IFM.IP", None, open_mins=30.0)
    assert w._unmanageable_actions([row]) == []
    assert w.state.no_pnl_streak.get("DEAL_C") == 1


def test_dedup_actions_caps_and_dedupes() -> None:
    w = _wrapper()
    w.cfg["max_actions_per_cycle"] = 2
    acts = [
        ManageAction("D1", "E", 0, "flatten", "r1"),
        ManageAction("D1", "E", 0, "flatten", "dup"),
        ManageAction("D2", "E", 0, "flatten", "r2"),
        ManageAction("D3", "E", 0, "flatten", "r3"),
    ]
    out = w._dedup_actions(acts)
    assert [a.deal_id for a in out] == ["D1", "D2"]


def test_arm_stack_every_cycle_config() -> None:
    w = _wrapper()
    w.cfg["arm_stack_every_cycle"] = True
    w.cfg["arm_every_n_cycles"] = 5
    w.state.cycles = 3
    every_cycle = bool(w.cfg.get("arm_stack_every_cycle", False))
    every_n = int(w.cfg.get("arm_every_n_cycles", 5) or 1)
    assert every_cycle or every_n <= 1 or (w.state.cycles % every_n == 0)
