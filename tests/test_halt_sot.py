"""Halt SoT — existence alone must not pause; only active:true blocks."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture()
def lane_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("IG_TEST_HARNESS", "1")
    for sub in ("state", "state_cfd", "state_sb"):
        (tmp_path / sub).mkdir(parents=True)
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("system.paths.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("runtime.halt_sot.data_dir", lambda: tmp_path, raising=False)
    # Patch via module that halt_sot imports inside functions.
    import runtime.halt_sot as halt_sot

    monkeypatch.setattr(
        "runtime.halt_sot._lane_state_roots",
        lambda: [tmp_path / "state", tmp_path / "state_cfd", tmp_path / "state_sb"],
    )
    return tmp_path


def _write(path: Path, active: bool, reason: str = "unit") -> None:
    path.write_text(
        json.dumps({"active": active, "reason": reason, "ts": time.time()}),
        encoding="utf-8",
    )


def test_missing_halt_file_not_active(lane_roots: Path) -> None:
    from runtime.halt_sot import any_entry_halt_active, flag_file_active

    p = lane_roots / "state" / "entry_halt.json"
    assert p.is_file() is False
    assert flag_file_active(p) is False
    assert any_entry_halt_active() is False


def test_active_false_does_not_pause(lane_roots: Path) -> None:
    from runtime.halt_sot import any_entry_halt_active, flag_file_active

    p = lane_roots / "state" / "entry_halt.json"
    _write(p, active=False, reason="cleared")
    assert flag_file_active(p) is False
    assert any_entry_halt_active() is False


def test_active_true_pauses_shared_and_lanes(lane_roots: Path) -> None:
    from runtime.halt_sot import active_halt_flags, any_entry_halt_active

    _write(lane_roots / "state_cfd" / "entry_halt.json", active=True, reason="cfd_halt")
    assert any_entry_halt_active() is True
    flags = active_halt_flags(include_deploy_hold=False)
    assert any(f.get("lane") == "state_cfd" for f in flags)


def test_deploy_hold_existence_without_active_not_held(
    lane_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.deploy_hold import deploy_hold_file_active

    # Bare file / empty active must not hold.
    hold = lane_roots / "state" / "deploy_hold.json"
    hold.write_text(json.dumps({"reason": "stale", "ts": time.time()}), encoding="utf-8")
    monkeypatch.setattr("runtime.deploy_hold._hold_path", lambda: hold)
    monkeypatch.setattr(
        "runtime.halt_sot._lane_state_roots",
        lambda: [lane_roots / "state", lane_roots / "state_cfd", lane_roots / "state_sb"],
    )
    assert deploy_hold_file_active() is False

    _write(hold, active=True, reason="operator")
    assert deploy_hold_file_active() is True


def test_resume_deletes_or_inactivates_all_lanes(
    lane_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("runtime.desk_dev_controls.data_dir", lambda: lane_roots)
    monkeypatch.setattr(
        "runtime.desk_dev_controls.state_dir", lambda: lane_roots / "state"
    )
    monkeypatch.setattr(
        "runtime.desk_dev_controls._is_production_state_path", lambda _p: False
    )
    monkeypatch.setattr(
        "runtime.deploy_hold._hold_path", lambda: lane_roots / "state" / "deploy_hold.json"
    )
    monkeypatch.setattr(
        "runtime.deploy_hold._is_production_state_path", lambda _p: False
    )

    for sub in ("state", "state_cfd", "state_sb"):
        _write(lane_roots / sub / "entry_halt.json", active=True)
        _write(lane_roots / sub / "trading_paused.json", active=True)
    _write(lane_roots / "state" / "deploy_hold.json", active=True)

    from runtime.desk_dev_controls import resume_entries
    from runtime.halt_sot import any_entry_halt_active

    out = resume_entries(reason="unit_halt_sot_resume")
    assert out["ok"] is True
    assert any_entry_halt_active() is False
    for sub in ("state", "state_cfd", "state_sb"):
        for name in ("entry_halt.json", "trading_paused.json"):
            path = lane_roots / sub / name
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                assert raw.get("active") is not True
