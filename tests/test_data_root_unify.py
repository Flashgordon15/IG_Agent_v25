"""Tier-0: data_dir follows IG_DATA_ROOT and bridges legacy artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_roots(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy_data"
    target = tmp_path / "v31-production"
    legacy.mkdir()
    (legacy / "state").mkdir()
    (legacy / "learning_db.sqlite3").write_bytes(b"REALDB" + b"\x00" * 64)
    (legacy / "trade_support_status.json").write_text(
        json.dumps({"broker_open": 1, "ts": 1.0}), encoding="utf-8"
    )
    (legacy / "state" / "broker_snapshot.json").write_text(
        json.dumps({"count": 1, "positions": []}), encoding="utf-8"
    )
    target.mkdir()
    (target / "learning_db.sqlite3").write_bytes(b"")  # empty stub
    monkeypatch.setenv("IG_DATA_ROOT", str(target))
    monkeypatch.delenv("IG_AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("IG_AGENT_LEGACY_DATA", "1")  # disable apex divert
    return legacy, target


def test_data_dir_follows_ig_data_root(isolated_roots, monkeypatch):
    from system import paths as paths_mod

    legacy, target = isolated_roots
    monkeypatch.setattr(paths_mod, "legacy_src_data_dir", lambda: legacy)
    monkeypatch.setattr(paths_mod, "_use_apex_isolated_store", lambda: False)

    d = paths_mod.data_dir()
    assert d.resolve() == target.resolve()
    assert (d / "state").is_dir()


def test_bridge_replaces_empty_learning_stub(isolated_roots, monkeypatch):
    from system import paths as paths_mod

    legacy, target = isolated_roots
    monkeypatch.setattr(paths_mod, "legacy_src_data_dir", lambda: legacy)

    actions = paths_mod.bridge_legacy_data_into(target, legacy=legacy)
    assert any("learning_db" in a for a in actions)
    dst = target / "learning_db.sqlite3"
    assert dst.is_symlink()
    assert dst.resolve() == (legacy / "learning_db.sqlite3").resolve()
    assert (target / "trade_support_status.json").is_file()
    assert (target / "state" / "broker_snapshot.json").is_file()


def test_last_good_positions_on_timeout_preserves_opens():
    from api import positions_live as pl

    pl._LAST_GOOD_PAYLOAD = None
    pl._LAST_GOOD_TS = 0.0
    good = {
        "ok": True,
        "count": 1,
        "positions": [{"deal_id": "D1", "pnl_gbp": -12.0}],
        "verdict": "DEGRADED",
        "stale": False,
        "critical": False,
        "critical_alarms": [],
        "trade_support": {"broker_open": 1},
        "total_pnl_gbp": -12.0,
    }
    pl.remember_live_positions_payload(good)
    out = pl.last_good_live_positions_payload(error="timeout")
    assert out["count"] == 1
    assert out["verdict"] == "CRITICAL"
    assert out["stale"] is True
    assert out["error"] == "timeout"
    assert out["positions"][0]["deal_id"] == "D1"
