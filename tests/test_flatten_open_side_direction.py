"""skip_lookup=True must receive OPEN side — close_position inverts once for DELETE."""

from __future__ import annotations

from unittest.mock import MagicMock

import runtime.dynamic_limit_engine as dle
from execution import exit_execution_gate as gate


def setup_function() -> None:
    dle.reset_dynamic_limit_for_tests()
    with gate._lock:
        gate._executing.clear()
        gate._paused.clear()
        gate._flatten_fail_counts.clear()
        gate._flatten_circuit_until.clear()


def teardown_function() -> None:
    dle.reset_dynamic_limit_for_tests()
    with gate._lock:
        gate._executing.clear()
        gate._paused.clear()
        gate._flatten_fail_counts.clear()
        gate._flatten_circuit_until.clear()


class _CaptureRest:
    """Captures close_position kwargs; optional payload invert mirror of IG client."""

    def __init__(self, items=None):
        self.items = items or []
        self.closes: list[dict] = []
        self.delete_directions: list[str] = []

    def open_positions(self, budget_priority=False):
        return list(self.items)

    def close_position(self, deal_id, **kwargs):
        self.closes.append({"deal_id": deal_id, **kwargs})
        open_side = str(kwargs.get("direction") or "").upper()
        # Mirror rest_client._do_close_position skip_lookup invert.
        delete_dir = "SELL" if open_side == "BUY" else "BUY"
        self.delete_directions.append(delete_dir)
        self.items = [
            it
            for it in self.items
            if str((it.get("position") or {}).get("dealId")) != deal_id
        ]
        return {
            "dealReference": "REF1",
            "verified_closed": True,
            "confirm": {"accepted": True, "dealStatus": "ACCEPTED"},
        }


def test_exit_gate_sell_open_passes_open_side_delete_is_buy():
    rest = _CaptureRest(
        [
            {
                "position": {
                    "dealId": "DISELL",
                    "direction": "SELL",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )
    result = gate.request_flatten(
        rest=rest,
        deal_id="DISELL",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        reason="unit_sell",
        source="unit",
    )
    assert result["ok"] is True
    assert len(rest.closes) == 1
    assert rest.closes[0]["direction"] == "SELL"  # OPEN side
    assert rest.closes[0]["skip_lookup"] is True
    assert rest.delete_directions == ["BUY"]  # IG DELETE direction


def test_dynamic_limit_flatten_passes_open_side_for_sell():
    dle.start_dynamic_limit_engine()
    dle.register_dynamic_limit(
        deal_id="DISELL2",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        entry_level=52000.0,
        limit_pts=2.0,
        size=0.5,
        trail_trigger_ig_pts=1.0,
    )
    rest = _CaptureRest(
        [
            {
                "position": {
                    "dealId": "DISELL2",
                    "direction": "SELL",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )
    dle.bind_rest_client(rest)
    track = dle._tracks["DISELL2"]
    dle._flatten_sync(track)
    assert len(rest.closes) == 1
    assert rest.closes[0]["direction"] == "SELL"
    assert rest.closes[0].get("skip_lookup") is True
    assert rest.delete_directions == ["BUY"]


def test_dynamic_limit_legacy_fallback_passes_open_not_close_dir(monkeypatch):
    """If exit_gate import path fails, legacy close_position still gets OPEN side."""
    dle.start_dynamic_limit_engine()
    dle.register_dynamic_limit(
        deal_id="DISELL3",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        entry_level=52000.0,
        limit_pts=2.0,
        size=0.5,
    )
    rest = MagicMock()
    rest.close_position.return_value = {"verified_closed": True}
    dle.bind_rest_client(rest)
    track = dle._tracks["DISELL3"]

    import builtins

    real_import = builtins.__import__

    def _block_gate(name, *args, **kwargs):
        if name == "execution.exit_execution_gate" or name.endswith(
            "exit_execution_gate"
        ):
            raise ImportError("blocked for unit test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_gate)
    dle._flatten_sync(track)
    rest.close_position.assert_called_once()
    kwargs = rest.close_position.call_args.kwargs
    assert kwargs["direction"] == "SELL"
    assert kwargs["skip_lookup"] is True


def test_flatten_circuit_stops_retry_after_n_failures():
    rest = _CaptureRest(
        [
            {
                "position": {
                    "dealId": "DIFAIL",
                    "direction": "SELL",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )

    def _fail_close(deal_id, **kwargs):
        rest.closes.append({"deal_id": deal_id, **kwargs})
        raise RuntimeError("validation boom")

    rest.close_position = _fail_close  # type: ignore[method-assign]
    for _ in range(gate._FLATTEN_FAIL_MAX):
        out = gate.request_flatten(
            rest=rest,
            deal_id="DIFAIL",
            epic="IX.D.DOW.IFM.IP",
            direction="SELL",
            size=0.5,
            reason="fail",
            source="unit",
        )
        assert out.get("ok") is False
    assert gate.flatten_circuit_open("DIFAIL") is True
    blocked = gate.request_flatten(
        rest=rest,
        deal_id="DIFAIL",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        reason="blocked",
        source="unit",
    )
    assert blocked.get("skipped") is True
    assert blocked.get("reason") == "flatten_circuit_open"
    assert len(rest.closes) == gate._FLATTEN_FAIL_MAX


def test_annotate_close_confirm_marks_opened_spawn():
    from ig_api.rest_client import IGRestClient

    data = {
        "dealReference": "REF",
        "confirm": {
            "accepted": True,
            "terminal": True,
            "dealStatus": "OPENED",
            "deal_id": "DINEW",
        },
    }
    out = IGRestClient._annotate_close_confirm(data)
    assert out.get("close_spawned") is True
    assert out.get("verified_closed") is False
    assert out["confirm"].get("opened") is True


def test_atomic_protect_passes_open_side(monkeypatch):
    from execution.scalping import atomic_protect as ap

    calls: list[dict] = []

    class _Client:
        def close_position(self, deal_id, **kwargs):
            calls.append({"deal_id": deal_id, **kwargs})

    ap.emergency_close_position(
        _Client(),
        deal_id="DI1",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        reason="unit",
    )
    assert len(calls) == 1
    assert calls[0]["direction"] == "SELL"  # OPEN side, not BUY close_dir


def test_micro_gbp_legacy_fallback_passes_open_side(monkeypatch):
    import runtime.micro_gbp_exit as mge

    rest = MagicMock()
    rest.close_position.return_value = {"verified_closed": True}
    monkeypatch.setattr(mge, "_resolve_rest_client", lambda: rest)
    track = mge.GbpExitTrack(
        deal_id="DIMicro",
        epic="IX.D.DOW.IFM.IP",
        direction="SELL",
        size=0.5,
        entry_level=52000.0,
        loss_cap_gbp=4.0,
        soft_loss_gbp=2.2,
        target_profit_gbp=8.5,
        trail_trigger_gbp=3.0,
        trail_lock_ratio=0.55,
    )
    mge._flatten_sync(track, reason="unit")
    rest.close_position.assert_called_once()
    kwargs = rest.close_position.call_args.kwargs
    assert kwargs["direction"] == "SELL"
    assert kwargs.get("skip_lookup") is True
