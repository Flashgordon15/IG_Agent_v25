"""ops_strip desk_idle_reason — one clear idle label for the Terminal."""

from __future__ import annotations

from api.routes import _desk_idle_reason_for_ops


def test_desk_idle_reason_us_close(monkeypatch) -> None:
    monkeypatch.setattr(
        "runtime.intraday_slot_tracker.intraday_slots_enabled",
        lambda cfg: True,
    )
    monkeypatch.setattr(
        "runtime.intraday_slot_tracker.slot_id_for_timestamp",
        lambda ts, cfg: "us_close",
    )
    monkeypatch.setattr(
        "system.config_loader.get_config",
        lambda: {},
    )
    reason = _desk_idle_reason_for_ops()
    assert reason is not None
    assert reason["code"] == "us_close"
    assert "US Close" in reason["label"]


def test_desk_idle_reason_insufficient_bars(monkeypatch) -> None:
    monkeypatch.setattr(
        "runtime.intraday_slot_tracker.intraday_slots_enabled",
        lambda cfg: False,
    )
    monkeypatch.setattr(
        "system.regime_state.get_regime_state_snapshot",
        lambda: {
            "markets": [
                {
                    "epic": "IX.D.DOW.IFM.IP",
                    "reason": "insufficient_bars",
                    "strategy_gate": {"allow_entries": False, "mode": "warmup"},
                }
            ]
        },
    )
    reason = _desk_idle_reason_for_ops()
    assert reason is not None
    assert reason["code"] == "insufficient_bars"
