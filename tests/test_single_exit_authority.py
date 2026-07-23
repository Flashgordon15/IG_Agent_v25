"""Micro soft/hard route through exit gate; hard floor fires without entry."""

from __future__ import annotations

from runtime import micro_gbp_exit as mge


def test_soft_loss_routes_to_flatten(monkeypatch):
    calls: list[str] = []

    def _fake_flatten(track, reason="", pnl_gbp=0.0, exit_meta=None):
        calls.append(reason)

    monkeypatch.setattr(mge, "_flatten", _fake_flatten)

    import system.config_loader as cl

    monkeypatch.setattr(
        cl,
        "get_config",
        lambda: {
            "broker_upl_hard_floor": {"enabled": True, "floor_gbp": -100.0},
            "loss_patience": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        "execution.exit_execution_gate.is_paused",
        lambda deal_id: False,
    )

    track = mge.GbpExitTrack(
        deal_id="DTEST2",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=45000.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=4.0,
        trail_trigger_gbp=2.0,
        trail_lock_ratio=0.7,
        armed_at=0.0,
    )
    with mge._lock:
        mge._tracks[track.deal_id] = track

    mge._evaluate_track(track, -50.0)
    assert any("soft_loss" in c for c in calls)

    with mge._lock:
        mge._tracks.pop(track.deal_id, None)


def test_hard_floor_without_entry(monkeypatch):
    calls: list[str] = []

    def _fake_flatten(track, reason="", pnl_gbp=0.0, exit_meta=None):
        calls.append(reason)

    monkeypatch.setattr(mge, "_flatten", _fake_flatten)

    import system.config_loader as cl

    monkeypatch.setattr(
        cl,
        "get_config",
        lambda: {
            "broker_upl_hard_floor": {"enabled": True, "floor_gbp": -100.0},
            "loss_patience": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        "execution.exit_execution_gate.is_paused",
        lambda deal_id: False,
    )

    track = mge.GbpExitTrack(
        deal_id="DTEST3",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=0.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=4.0,
        trail_trigger_gbp=2.0,
        trail_lock_ratio=0.7,
        armed_at=0.0,
    )
    with mge._lock:
        mge._tracks[track.deal_id] = track

    mge._evaluate_track(track, -122.35)
    assert any("hard_floor" in c for c in calls)

    with mge._lock:
        mge._tracks.pop(track.deal_id, None)


def test_paused_deal_skips(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        mge, "_flatten", lambda *a, **k: calls.append("x")
    )
    monkeypatch.setattr(
        "execution.exit_execution_gate.is_paused",
        lambda deal_id: True,
    )
    import system.config_loader as cl

    monkeypatch.setattr(cl, "get_config", lambda: {})

    track = mge.GbpExitTrack(
        deal_id="DPAUSE",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        entry_level=45000.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=4.0,
        trail_trigger_gbp=2.0,
        trail_lock_ratio=0.7,
        armed_at=0.0,
    )
    with mge._lock:
        mge._tracks[track.deal_id] = track
    mge._evaluate_track(track, -50.0)
    assert calls == []
    with mge._lock:
        mge._tracks.pop(track.deal_id, None)
