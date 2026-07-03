"""Iron Ledger stability — lock-free API reads under concurrent load + transport stalls."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime import master_orchestrator as mo
from system import chaos_guardian as cg


@pytest.fixture(autouse=True)
def _isolate():
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()


def test_iron_ledger_commit_is_immutable_copy():
    payload = {
        "ts": time.time(),
        "platform_pp": 1200,
        "token_buckets": {"ig_orders": {"tokens": 2.0}},
        "position_tree": [{"epic": "IX.D.DOW.IFM.IP"}],
        "orchestrator": {"ok": True, "ts": time.time(), "scoreboard": {"total_pp": 1200}},
        "guardian": {"ok": True, "healthy": True, "ts": time.time()},
    }
    ver = cg.IronLedgerSnapshot.commit(payload)
    assert ver >= 1
    read_back = cg.read_iron_ledger_snapshot()
    payload["platform_pp"] = 1
    assert read_back["platform_pp"] == 1200
    assert cg.read_iron_ledger_orchestrator().get("ok") is True


def test_api_snapshots_read_ledger_without_composing():
    cg.seed_iron_ledger_for_tests(
        orchestrator={"ok": True, "healthy": True, "primed": True, "ts": time.time()},
        guardian={"ok": True, "healthy": True, "ts": time.time()},
    )
    with patch.object(mo, "_compose_orchestrator_snapshot_body") as compose:
        with patch.object(cg, "build_guardian_snapshot_body") as build:
            orch = mo.get_orchestrator_state_snapshot()
            guard = cg.get_guardian_status_snapshot()
    compose.assert_not_called()
    build.assert_not_called()
    assert orch.get("primed") is True
    assert guard.get("healthy") is True


def test_dispatch_drops_poison_frame_without_crashing_dispatcher():
    async def _run():
        bad = await mo._dispatch_single_update("IX.D.NIKKEI.IFM.IP", float("nan"), 100.0)
        good = await mo._dispatch_single_update(
            "CS.D.CFPGOLD.CFP.IP",
            2400.0,
            2400.5,
        )
        return bad, good

    with patch.object(mo, "resolve_execution_route") as resolve:
        resolve.return_value = mo.RouteDecision(
            epic="CS.D.CFPGOLD.CFP.IP",
            regime_state=1,
            regime_label="trend",
            execution_path="limit_chase_hf",
            allow_entry=True,
            size_factor_mult=1.0,
            stop_factor_mult=1.0,
            kelly_fraction=0.1,
            confidence=80.0,
            reason="test",
        )
        bad, good = asyncio.run(_run())
    assert bad is None
    assert good is not None
    assert mo._asset_status.get("IX.D.NIKKEI.IFM.IP") == "DEGRADED"
    assert mo._asset_status.get("CS.D.CFPGOLD.CFP.IP") == "HEALTHY"


def test_concurrent_api_reads_under_ledger_publish():
    publish_stop = threading.Event()
    errors: list[str] = []

    def _publisher():
        while not publish_stop.is_set():
            try:
                mo.publish_iron_ledger_snapshot()
            except Exception as exc:
                errors.append(f"publish:{exc}")
            time.sleep(0.002)

    def _reader(_: int) -> dict:
        return {
            "orch": mo.get_orchestrator_state_snapshot(),
            "guard": cg.get_guardian_status_snapshot(),
        }

    pub = threading.Thread(target=_publisher, name="ledger-publisher", daemon=True)
    pub.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(_reader, i) for i in range(500)]
        results = [f.result(timeout=5.0) for f in futures]
    publish_stop.set()
    pub.join(timeout=2.0)
    assert not errors
    assert len(results) == 500
    assert all(isinstance(r["orch"], dict) for r in results)


def test_trading_loop_tick_budget_under_transport_stall(monkeypatch):
    tick_times: list[float] = []

    def _fake_dispatch(updates):
        tick_times.append(time.perf_counter())
        return []

    async def _fake_dispatch_async(updates):
        return _fake_dispatch(updates)

    monkeypatch.setattr(mo, "dispatch_market_updates", _fake_dispatch_async)

    stall = {"active": True}

    def _stall_feed_state():
        if stall["active"]:
            raise TimeoutError("simulated_transport_stall")
        return {"health": "ok", "fresh_count": 6, "total_epics": 7}

    monkeypatch.setattr(
        "system.feeds.data_feed_orchestrator.get_data_feed_state",
        _stall_feed_state,
    )

    async def _burst():
        for _ in range(40):
            await mo.dispatch_market_updates([("IX.D.DOW.IFM.IP", 42000.0, 42001.0)])
            await asyncio.sleep(0)

    t0 = time.perf_counter()
    asyncio.run(_burst())
    elapsed = time.perf_counter() - t0
    stall["active"] = False
    assert len(tick_times) == 40
    assert elapsed < 2.0


def test_sequential_desktop_teardown_within_one_second(monkeypatch):
    from cockpit.desktop_process_guard import (
        execute_sequential_desktop_teardown,
        reset_desktop_process_guard_for_tests,
    )

    reset_desktop_process_guard_for_tests()
    monkeypatch.setattr(
        "cockpit.desktop_process_guard._transmit_broker_disconnect_frame",
        lambda: {"ok": True, "flushed": 1},
    )
    monkeypatch.setattr("cockpit.desktop_process_guard._pids_on_port", lambda _p: [])
    monkeypatch.setattr(
        "system.shutdown_cleanup.mark_manual_stop",
        lambda **kw: None,
    )
    summary = execute_sequential_desktop_teardown(skip_mark_manual_stop=False, deadline_sec=1.0)
    assert summary.get("ok") is True
    assert summary.get("manual_stop") is True
    assert float(summary.get("elapsed_ms", 9999)) <= 1100.0
