"""Alert reporting matrix — queue, routing, EOD summary tests."""

from __future__ import annotations

import time

import pytest

from system import alert_reporting_matrix as arm


@pytest.fixture(autouse=True)
def _isolate():
    arm.reset_alert_reporting_for_tests()
    yield
    arm.reset_alert_reporting_for_tests()


def test_enqueue_non_blocking():
    ok = arm.broadcast_critical_event_async(
        category=arm.EventCategory.SYSTEM_SECURITY,
        title="Test",
        body="Body",
        priority=arm.EventPriority.CRITICAL,
    )
    assert ok is True
    assert arm._event_queue.qsize() >= 1


def test_event_format_critical():
    event = arm.AlertEvent(
        category=arm.EventCategory.SYSTEM_SECURITY,
        priority=arm.EventPriority.CRITICAL,
        title="CB Trip",
        body="L2 active",
        metadata={"ticker": "CS.D.EURUSD.CFD.IP", "slippage_pts": 0.42},
    )
    msg = event.format_message()
    assert "🚨" in msg
    assert "CB Trip" in msg
    assert "CS.D.EURUSD.CFD.IP" in msg
    assert "0.420" in msg or "0.42" in msg


def test_priority_routing_critical():
    arm.notify_circuit_breaker_trip(level=2, drawdown_pct=4.5)
    event = arm._event_queue.get(timeout=1.0)
    assert event.priority == arm.EventPriority.CRITICAL
    assert event.category == arm.EventCategory.SYSTEM_SECURITY


def test_pp_boundary_expansion_alert():
    arm.notify_pp_boundary_crossing(1150, 1210)
    event = arm._event_queue.get(timeout=1.0)
    assert event.category == arm.EventCategory.GAMIFICATION
    assert event.priority == arm.EventPriority.INFO
    assert "Expansion" in event.title


def test_pp_boundary_defensive_alert():
    arm._last_pp_tier = "standard"
    arm.notify_pp_boundary_crossing(850, 790)
    event = arm._event_queue.get(timeout=1.0)
    assert "Defensive" in event.title


def test_worker_dispatches_and_logs(monkeypatch):
    monkeypatch.setattr(arm, "_telegram_send", lambda m: True)
    monkeypatch.setattr(arm, "_discord_send", lambda m: True)
    arm.broadcast_critical_event_async(
        category=arm.EventCategory.SYSTEM_SECURITY,
        title="Flat",
        body="done",
    )
    item = arm._event_queue.get(timeout=1.0)
    arm._dispatch_webhooks(item)
    arm._record_broadcast(item, delivered=True)
    snap = arm.get_reporting_status_snapshot()
    assert snap["queue_depth"] >= 0
    assert len(snap.get("last_broadcasts") or []) >= 1


def test_compile_eod_summary(monkeypatch):
    monkeypatch.setattr(
        "runtime.parameter_tuner.harvest_closed_trades",
        lambda since_ts=None: [
            {"net_pnl": 50.0},
            {"net_pnl": -20.0},
            {"net_pnl": 30.0},
        ],
    )
    monkeypatch.setattr("runtime.parameter_tuner._slippage_by_epic", lambda since_ts=None: {"E": 0.5})
    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine.get_exploration_state_snapshot",
        lambda: {"capital_allocation_pct": 42.0},
    )
    summary = arm.compile_eod_summary()
    assert summary["total_trades"] == 3
    assert summary["win_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert summary["net_pnl_gbp"] == 60.0
    assert summary["peak_margin_utilization_pct"] == 42.0


def test_unconfigured_webhooks_report_idle_healthy(monkeypatch):
    monkeypatch.setattr(arm, "_telegram_configured", lambda: False)
    monkeypatch.setattr(arm, "_discord_configured", lambda: False)
    arm.start_alert_reporting_matrix()
    time.sleep(0.05)
    assert arm.reporting_healthy() is True
    snap = arm.get_reporting_status_snapshot()
    assert snap["subsystem_status"] == "IDLE"
    assert snap["webhooks"]["telegram"]["state"] in ("IDLE", "SKIPPED")
    assert snap["webhooks"]["discord"]["state"] in ("IDLE", "SKIPPED")


def test_reporting_status_snapshot_fields():
    snap = arm.get_reporting_status_snapshot()
    assert "webhooks" in snap
    assert "queue_depth" in snap
    assert "last_broadcasts" in snap
    assert "healthy" in snap


def test_milestone_markdown_includes_pp_metadata(monkeypatch):
    monkeypatch.setattr(
        "runtime.master_orchestrator.get_platform_scoreboard",
        lambda: type(
            "SB",
            (),
            {
                "rank_label": lambda self: "aggressive",
                "capacity_multiplier": lambda self: 1.12,
            },
        )(),
    )
    arm.notify_pp_boundary_crossing(1150, 1210)
    event = arm._event_queue.get(timeout=1.0)
    msg = event.format_message()
    assert "🟢" in msg
    assert "Platform PP" in msg
    assert "aggressive" in msg


def test_critical_bypasses_coalesce_buffer(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(arm, "_telegram_send", lambda m, **kw: sent.append(m) or True)
    monkeypatch.setattr(arm, "_discord_send", lambda m: True)
    arm.set_coalesce_window_for_tests(5.0)
    arm.start_alert_reporting_matrix()
    arm.notify_circuit_breaker_trip(level=2, drawdown_pct=3.5, ticker="IX.D.DOW.IFM.IP")
    deadline = time.time() + 2.0
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    assert len(sent) == 1
    assert "🚨" in sent[0]
    assert "IX.D.DOW.IFM.IP" in sent[0]


def test_burst_50_scalper_events_coalesce_without_data_loss(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(arm, "_telegram_send", lambda m, **kw: sent.append(m) or True)
    monkeypatch.setattr(arm, "_discord_send", lambda m: True)
    monkeypatch.setattr(
        arm,
        "_buffer_coalesced_telegram_digest",
        lambda msg: sent.append(msg) or True,
    )
    arm.set_coalesce_window_for_tests(0.2)
    arm.start_alert_reporting_matrix()

    titles: list[str] = []
    for i in range(50):
        title = f"Scalp fill #{i}"
        titles.append(title)
        arm.notify_scalper_trade_event(
            ticker="CS.D.EURUSD.CFD.IP",
            title=title,
            body=f"Micro-scalp exit {i} slippage={0.1 + i * 0.01:.2f}pts",
            slippage_pts=0.1 + i * 0.01,
        )

    deadline = time.time() + 3.0
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    arm.flush_coalesce_buffer_for_tests()
    deadline2 = time.time() + 1.0
    while time.time() < deadline2 and not sent:
        time.sleep(0.05)

    assert len(sent) >= 1
    combined = "\n".join(sent)
    assert "50 events" in combined or "Batch Status" in combined
    for title in titles:
        assert title in combined

    snap = arm.get_reporting_status_snapshot()
    assert snap.get("coalesce_batches_sent", 0) >= 1
    with arm._lock:
        delivered = [b for b in arm._broadcast_log if b.get("delivered")]
    assert len(delivered) == 50


def test_auto_tuning_digest_enqueue():
    arm.notify_auto_tuning_digest(
        matrix={"0": {"size_factor": 0.8, "stop_factor": 0.9, "limit_factor": 0.85}},
        history=[{"ts": time.time()}],
    )
    event = arm._event_queue.get(timeout=1.0)
    assert event.category == arm.EventCategory.AUTO_TUNING
    assert event.priority == arm.EventPriority.DEBUG
