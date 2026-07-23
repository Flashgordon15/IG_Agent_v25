"""Emergency kill API handler."""

from __future__ import annotations

from api.emergency_kill import run_emergency_kill


def test_run_emergency_kill_flattens(monkeypatch):
    closed: list[str] = []

    class Rest:
        def cancel_all_working_orders(self):
            return 2

        def open_positions(self, budget_priority=False):
            if closed:
                return []
            return [
                {
                    "position": {
                        "dealId": "DKILL",
                        "direction": "BUY",
                        "size": 0.5,
                    },
                    "market": {"epic": "IX.D.DOW.IFM.IP"},
                }
            ]

    monkeypatch.setattr(
        "system.shutdown_cleanup.mark_manual_stop",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "api.agent_control.stop_trading",
        lambda: {"ok": True, "status": "stopped"},
    )

    class Cred:
        ok = True
        credentials = object()
        error = None

    monkeypatch.setattr(
        "system.credentials_loader.try_load_credentials",
        lambda: Cred(),
    )
    monkeypatch.setattr(
        "system.config_loader.load_active_config",
        lambda validate=False: type("C", (), {"currency_code": "GBP"})(),
    )
    monkeypatch.setattr(
        "system.ig_rest_session.ensure_shared_authenticated",
        lambda cred: Rest(),
    )

    def _flatten(**kwargs):
        closed.append(kwargs["deal_id"])
        return {"ok": True, "deal_id": kwargs["deal_id"]}

    monkeypatch.setattr(
        "execution.exit_execution_gate.request_flatten",
        _flatten,
    )
    monkeypatch.setattr(
        "execution.exit_execution_gate.set_emergency_kill_active",
        lambda v: None,
    )

    report = run_emergency_kill(source="test")
    assert report["loops_stopped"] is True
    assert report["cancelled_orders"] == 2
    assert "DKILL" in report["closed"]
    assert report["ok"] is True
