"""Legacy snapshot-mirror deactivation when book is flat."""

from __future__ import annotations

from pathlib import Path

from runtime.snapshot_mirror_cleanup import (
    force_snapshot_sync_route_deployed,
    maybe_deactivate_legacy_snapshot_mirror,
    stop_flag_path,
)


def test_route_deployed_detects_source() -> None:
    assert force_snapshot_sync_route_deployed() is True


def test_skips_when_positions_open(tmp_path: Path, monkeypatch) -> None:
    flag = tmp_path / ".stop_snapshot_mirror"
    monkeypatch.setattr(
        "runtime.snapshot_mirror_cleanup.stop_flag_path", lambda: flag
    )
    monkeypatch.setattr(
        "runtime.snapshot_mirror_cleanup.force_snapshot_sync_route_deployed",
        lambda: True,
    )
    out = maybe_deactivate_legacy_snapshot_mirror(open_count=1)
    assert out["ok"] is False
    assert out["skipped"] == "positions_open"
    assert not flag.exists()


def test_writes_stop_flag_when_flat(tmp_path: Path, monkeypatch) -> None:
    flag = tmp_path / ".stop_snapshot_mirror"
    monkeypatch.setattr(
        "runtime.snapshot_mirror_cleanup.stop_flag_path", lambda: flag
    )
    monkeypatch.setattr(
        "runtime.snapshot_mirror_cleanup.force_snapshot_sync_route_deployed",
        lambda: True,
    )
    monkeypatch.setattr(
        "runtime.snapshot_mirror_cleanup._reap_mirror_pids", lambda: []
    )
    out = maybe_deactivate_legacy_snapshot_mirror(open_count=0)
    assert out["ok"] is True
    assert flag.is_file()
