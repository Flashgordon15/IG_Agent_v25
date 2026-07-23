"""P0: snapshot coalesce fallback + hard pre-entry cap gate."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import runtime.broker_snapshot as bs
from execution.execution_engine import ExecutionEngine
from system.rest_api_budget import entries_blocked_by_rest_pressure


def _patch_snapshot_root(monkeypatch, tmp_path: Path) -> Path:
    primary = tmp_path / "broker_snapshot.json"

    def _paths() -> list[Path]:
        return [primary]

    monkeypatch.setattr(bs, "snapshot_path", lambda: primary)
    monkeypatch.setattr(bs, "_mirror_paths", _paths)
    return primary


def test_ig_items_from_snapshot_roundtrip(tmp_path, monkeypatch) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    positions = [
        {
            "deal_id": "D1",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "SELL",
            "size": 0.5,
            "entry": 52000.0,
            "pnl_gbp": 1.5,
        },
        {
            "deal_id": "D2",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "SELL",
            "size": 0.5,
            "entry": 52001.0,
            "pnl_gbp": -0.5,
        },
    ]
    assert bs.write_snapshot(source="test", positions=positions) is True
    items = bs.ig_items_from_snapshot(max_age_sec=None)
    assert len(items) == 2
    assert items[0]["position"]["dealId"] == "D1"
    assert items[0]["position"]["direction"] == "SELL"
    assert items[0]["market"]["epic"] == "IX.D.DOW.IFM.IP"
    assert items[0].get("_from_snapshot") is True
    assert bs.open_count_from_snapshot(max_age_sec=60.0) == 2


def test_remove_deals_from_snapshot(tmp_path, monkeypatch) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    bs.write_snapshot(
        source="test",
        positions=[
            {
                "deal_id": "D1",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "SELL",
                "size": 0.5,
                "entry": 1.0,
            },
            {
                "deal_id": "D2",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "SELL",
                "size": 0.5,
                "entry": 2.0,
            },
        ],
    )
    assert bs.remove_deals_from_snapshot(["D1"]) == 1
    snap = bs.read_snapshot(max_age_sec=None)
    assert snap is not None
    assert snap["count"] == 1
    assert snap["positions"][0]["deal_id"] == "D2"


def test_open_positions_returns_snapshot_under_coalesce(tmp_path, monkeypatch) -> None:
    """rest.open_positions must not raise coalesce — return last-good items."""
    _patch_snapshot_root(monkeypatch, tmp_path)
    bs.write_snapshot(
        source="test",
        positions=[
            {
                "deal_id": "DX",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "SELL",
                "size": 0.5,
                "entry": 100.0,
            }
        ],
    )
    monkeypatch.setattr(
        "system.rest_api_budget.positions_poll_deferred",
        lambda **kwargs: True,
    )
    from ig_api.rest_client import IGRestClient

    # Minimal instance: only need ensure_session + open_positions method body.
    rest = object.__new__(IGRestClient)
    rest.ensure_session = lambda: None  # type: ignore[method-assign]
    items = IGRestClient.open_positions(rest, budget_priority=True)
    assert len(items) == 1
    assert items[0]["position"]["dealId"] == "DX"
    assert items[0].get("_from_snapshot") is True


def test_pre_entry_blocks_on_snapshot_cap(monkeypatch, tmp_path) -> None:
    _patch_snapshot_root(monkeypatch, tmp_path)
    # 7 opens > max 6
    positions = [
        {
            "deal_id": f"D{i}",
            "epic": "IX.D.DOW.IFM.IP",
            "direction": "SELL",
            "size": 0.5,
            "entry": 100.0 + i,
        }
        for i in range(7)
    ]
    bs.write_snapshot(source="test", positions=positions)

    cfg = SimpleNamespace(
        max_open_positions=6,
        max_positions_per_epic=2,
        adaptive_execution_enabled=False,
    )
    # Minimal engine stub — only call _pre_entry_position_check
    eng = object.__new__(ExecutionEngine)
    eng.config = cfg
    eng._position_sync = None
    eng._tracker = SimpleNamespace(
        count_open_for_epic=lambda epic: 0,
        count_open_total=lambda: 0,
    )
    monkeypatch.setattr(
        "trading.position_ladder.base_max_per_epic",
        lambda cfg: 2,
    )
    signal = SimpleNamespace(epic="IX.D.DOW.IFM.IP")
    blocked, reason = eng._pre_entry_position_check(signal)
    assert blocked is True
    assert "broker_snapshot open=7" in reason


def test_entries_blocked_by_rest_pressure_elevated(monkeypatch) -> None:
    monkeypatch.setattr(
        "system.rest_api_budget._demo_throughput_rest_bypass",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget",
        lambda: SimpleNamespace(
            metrics=lambda: {"pressure_level": "ELEVATED", "status_label": "warn"}
        ),
    )
    monkeypatch.setattr(
        "system.rest_api_budget.positions_poll_deferred",
        lambda **kwargs: False,
    )
    blocked, reason = entries_blocked_by_rest_pressure()
    assert blocked is True
    assert "elevated" in reason


def test_demo_throughput_does_not_block_entries_on_elevated(monkeypatch) -> None:
    monkeypatch.setattr(
        "system.rest_api_budget._demo_throughput_rest_bypass",
        lambda: True,
    )
    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget",
        lambda: SimpleNamespace(
            metrics=lambda: {"pressure_level": "ELEVATED", "status_label": "warn"}
        ),
    )
    monkeypatch.setattr(
        "system.shared_rest_budget.recent_count",
        lambda bucket: 0,
    )
    blocked, reason = entries_blocked_by_rest_pressure()
    assert blocked is False
    assert reason == ""


def test_entries_not_blocked_when_idle(monkeypatch) -> None:
    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget",
        lambda: SimpleNamespace(
            metrics=lambda: {"pressure_level": "OK", "status_label": "ok"}
        ),
    )
    monkeypatch.setattr(
        "system.rest_api_budget.positions_poll_deferred",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        "system.shared_rest_budget.recent_count",
        lambda bucket: 0,
    )
    blocked, reason = entries_blocked_by_rest_pressure()
    assert blocked is False
    assert reason == ""


def test_entries_not_blocked_by_designed_positions_soft_cap(monkeypatch) -> None:
    """Healthy OK @ 2 pos/min must not false-red path_live via coalesce."""
    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget",
        lambda: SimpleNamespace(
            metrics=lambda: {
                "pressure_level": "OK",
                "status_label": "OK (2/min)",
                "by_category_last_minute": {"positions": 2},
            }
        ),
    )
    monkeypatch.setattr(
        "system.rest_api_budget.positions_poll_deferred",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "system.shared_rest_budget.recent_count",
        lambda bucket: 2,  # at soft cap, not storm
    )
    blocked, reason = entries_blocked_by_rest_pressure()
    assert blocked is False
    assert reason == ""


def test_entries_blocked_on_positions_storm(monkeypatch) -> None:
    monkeypatch.setattr(
        "system.rest_api_budget._demo_throughput_rest_bypass",
        lambda: False,
    )
    monkeypatch.setattr(
        "system.rest_api_budget.get_rest_api_budget",
        lambda: SimpleNamespace(
            metrics=lambda: {"pressure_level": "OK", "status_label": "ok"}
        ),
    )
    monkeypatch.setattr(
        "system.shared_rest_budget.recent_count",
        lambda bucket: 5,  # > 2x soft cap
    )
    blocked, reason = entries_blocked_by_rest_pressure()
    assert blocked is True
    assert reason == "rest_positions_coalesce_pressure"
