"""B2 — single exit execution gate lock."""

from __future__ import annotations

from execution import exit_execution_gate as gate


class _FakeRest:
    def __init__(self, items=None):
        self.items = items or []
        self.closes: list[tuple] = []

    def open_positions(self, budget_priority=False):
        return list(self.items)

    def close_position(self, deal_id, **kwargs):
        self.closes.append((deal_id, kwargs))
        # Remove from book after close
        self.items = [
            it
            for it in self.items
            if str((it.get("position") or {}).get("dealId")) != deal_id
        ]
        return {
            "dealReference": "REF",
            "verified_closed": True,
            "confirm": {"accepted": True, "dealStatus": "ACCEPTED"},
        }


def test_request_flatten_closes_and_reconciles():
    with gate._lock:
        gate._flatten_fail_counts.clear()
        gate._flatten_circuit_until.clear()
        gate._executing.clear()
    rest = _FakeRest(
        [
            {
                "position": {
                    "dealId": "DIAAA",
                    "direction": "BUY",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )
    result = gate.request_flatten(
        rest=rest,
        deal_id="DIAAA",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        reason="test",
        pnl_gbp=-122.0,
        source="unit",
    )
    assert result["ok"] is True
    assert len(rest.closes) == 1
    assert rest.closes[0][1]["direction"] == "BUY"  # OPEN side
    assert rest.closes[0][1]["skip_lookup"] is True
    assert result["reconcile"]["broker_open"] == 0


def test_is_executing_blocks_second_call(monkeypatch):
    rest = _FakeRest(
        [
            {
                "position": {
                    "dealId": "DIBBB",
                    "direction": "BUY",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )

    entered = []

    def _slow_close(deal_id, **kwargs):
        entered.append(deal_id)
        # Simulate concurrent second request while first holds lock
        second = gate.request_flatten(
            rest=rest,
            deal_id="DIBBB",
            epic="IX.D.DOW.IFM.IP",
            direction="BUY",
            size=0.5,
            reason="race",
            source="unit2",
        )
        assert second.get("skipped") is True
        rest.items = []
        return {
            "dealReference": "REF",
            "verified_closed": True,
            "confirm": {"accepted": True, "dealStatus": "ACCEPTED"},
        }

    monkeypatch.setattr(rest, "close_position", _slow_close)
    first = gate.request_flatten(
        rest=rest,
        deal_id="DIBBB",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        reason="primary",
        source="unit",
    )
    assert first["ok"] is True
    assert entered == ["DIBBB"]


def test_already_flat_is_ok():
    rest = _FakeRest([])
    result = gate.request_flatten(
        rest=rest,
        deal_id="GONE",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        reason="gone",
        source="unit",
    )
    assert result["ok"] is True
    assert result.get("already_flat") is True


def test_opened_confirm_never_succeeds_even_if_verified_closed():
    """Ghost path: net-close spawn returns OPENED — must abort, not ok=True."""
    with gate._lock:
        gate._flatten_fail_counts.clear()
        gate._flatten_circuit_until.clear()
        gate._executing.clear()

    rest = _FakeRest(
        [
            {
                "position": {
                    "dealId": "DIGHOST",
                    "direction": "BUY",
                    "size": 0.5,
                },
                "market": {"epic": "IX.D.DOW.IFM.IP"},
            }
        ]
    )

    def _spawn_close(deal_id, **kwargs):
        # Original deal disappears (ghost) while confirm OPENED a NEW position.
        rest.items = []
        return {
            "dealReference": "REF-SPAWN",
            "verified_closed": True,  # original dealId absent — must not win
            "close_spawned": True,
            "confirm": {
                "accepted": True,
                "terminal": True,
                "opened": True,
                "dealStatus": "OPENED",
                "status": "OPENED",
                "deal_id": "DINEWSPAWN",
            },
        }

    rest.close_position = _spawn_close  # type: ignore[method-assign]
    result = gate.request_flatten(
        rest=rest,
        deal_id="DIGHOST",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
        size=0.5,
        reason="ghost_spawn",
        source="unit",
    )
    assert result["ok"] is False
    assert result["error"] == "close_confirm_opened_spawn"
    assert gate.is_executing("DIGHOST") is False


def test_close_confirm_verdict_rejects_opened():
    ok, status, err = gate._close_confirm_verdict(
        {"accepted": True, "terminal": True, "status": "OPENED"}
    )
    assert ok is False
    assert status == "OPENED"
    assert err == "close_confirm_opened_spawn"
    ok2, status2, err2 = gate._close_confirm_verdict(
        {"accepted": True, "dealStatus": "FULLY_CLOSED"}
    )
    assert ok2 is True
    assert status2 == "FULLY_CLOSED"
    assert err2 == ""
