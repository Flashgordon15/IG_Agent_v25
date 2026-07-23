"""trade_support SoT — never publish broker_open=0 when snapshot has opens."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_status_overlay_when_rows_empty_snapshot_open(tmp_path, monkeypatch):
    from runtime import broker_snapshot
    from runtime import trade_support_wrapper as tsw

    snap_dir = tmp_path / "state"
    snap_dir.mkdir()
    monkeypatch.setattr(broker_snapshot, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(broker_snapshot, "legacy_src_data_dir", lambda: tmp_path)
    monkeypatch.setattr(tsw, "data_dir", lambda: tmp_path)
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("system.paths.legacy_src_data_dir", lambda: tmp_path)

    broker_snapshot.write_snapshot(
        source="test",
        positions=[
            {
                "deal_id": "DIAAAAOPEN1",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 42000.0,
                "pnl_gbp": -12.0,
            }
        ],
    )

    # Simulate coalesce status write path
    status = {
        "ts": 1.0,
        "cycles": 1,
        "source": "last_good_snapshot(test)",
        "broker_open": 0,
        "valued": 0,
        "unvalued": 0,
        "total_unrealized_gbp": 0.0,
        "by_epic": {},
        "actions_executed": 0,
        "actions": [],
        "issues": [],
        "flattened_total": 0,
        "edits_only_queue": {},
    }
    snap = broker_snapshot.read_snapshot(max_age_sec=None) or {}
    snap_count = int(snap.get("count") or len(snap.get("positions") or []))
    assert snap_count == 1
    if status["broker_open"] == 0 and snap_count > 0:
        status["broker_open"] = snap_count
        status["source"] = f"{status['source']}|sot_overlay_snapshot"
    tsw._write_status(status)

    path = tmp_path / "trade_support_status.json"
    body = json.loads(path.read_text())
    assert body["broker_open"] == 1
    assert "sot_overlay" in body["source"]


def test_api_trade_support_status_sot_overlay(tmp_path, monkeypatch):
    from runtime import broker_snapshot
    from api import routes

    monkeypatch.setattr(broker_snapshot, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(broker_snapshot, "legacy_src_data_dir", lambda: tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    broker_snapshot.write_snapshot(
        source="test",
        positions=[
            {
                "deal_id": "DIAAAAOPEN2",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "SELL",
                "size": 0.5,
                "entry": 42100.0,
                "pnl_gbp": 1.5,
            }
        ],
    )
    status_path = tmp_path / "trade_support_status.json"
    status_path.write_text(
        json.dumps(
            {
                "ts": __import__("time").time(),
                "broker_open": 0,
                "source": "cycle_heartbeat",
                "cycles": 1,
            }
        )
    )

    monkeypatch.setattr(
        "system.paths.data_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "system.paths.legacy_src_data_dir",
        lambda: tmp_path,
    )

    out = routes.api_trade_support_status()
    assert out.get("ok") is True
    assert int(out.get("broker_open") or 0) == 1
    assert out.get("sot_overlay") is True


def test_desk_dev_pause_resume(tmp_path, monkeypatch):
    from runtime import desk_dev_controls as ddc

    monkeypatch.setattr(ddc, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(ddc, "data_dir", lambda: tmp_path)
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)

    paused = ddc.pause_entries(reason="unit_test")
    assert paused["ok"] is True
    assert ddc.entries_paused() is True
    halt = json.loads((tmp_path / "entry_halt.json").read_text())
    assert halt["active"] is True
    # Hold lands in tmp — never v31-production
    hold = tmp_path / "state" / "deploy_hold.json"
    assert hold.is_file()
    assert "unit_test" in hold.read_text()

    resumed = ddc.resume_entries(reason="unit_test_resume")
    assert resumed["ok"] is True
    assert ddc.entries_paused() is False
