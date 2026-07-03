"""Phase 2 institutional hardening — zero-copy, spread fuse, multi-horizon, hot-swap."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from signals.signal_engine import SignalResult


@pytest.fixture(autouse=True)
def _reset_institutional_state():
    import runtime.master_orchestrator as mo
    import runtime.portfolio_exploration_engine as ppe
    import system.autonomic_healer as ah
    import system.chaos_guardian as cg
    from trading.probability_engine import reset_multi_horizon_cache_for_tests

    mo.reset_master_orchestrator_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    ah.reset_autonomic_healer_for_tests()
    cg.reset_chaos_guardian_for_tests()
    reset_multi_horizon_cache_for_tests()
    yield
    mo.reset_master_orchestrator_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    ah.reset_autonomic_healer_for_tests()
    cg.reset_chaos_guardian_for_tests()
    reset_multi_horizon_cache_for_tests()


def test_zero_copy_ring_push_drain_no_dict_alloc():
    from system.market_data_hub import _ZeroCopyStreamRing

    ring = _ZeroCopyStreamRing(capacity=64)
    epic = "CS.D.EURUSD.CFD.IP"
    for i in range(20):
        ok = ring.push(epic, 1.1000 + i * 0.0001, 1.1002 + i * 0.0001, source="websocket")
        assert ok is True
    batch = ring.drain_batch(max_items=32)
    assert batch.dtype.names is not None
    assert len(batch) == 20
    assert float(batch[0]["bid"]) > 0
    assert ring.epic_for_id(int(batch[0]["epic_id"])) == epic


def test_binary_ohlc_cache_roundtrip_under_100ms(tmp_path: Path, monkeypatch):
    from system.market_data_hub import load_binary_ohlc_cache, write_binary_ohlc_cache

    epic = "IX.D.DOW.IFM.IP"
    n = 288
    high = np.linspace(40000, 40100, n, dtype=np.float64)
    low = high - 25.0
    close = (high + low) / 2.0
    spreads = np.full(n, 1.5, dtype=np.float64)

    cache_file = tmp_path / "wall_street_5m.bin"
    monkeypatch.setattr(
        "trading.ohlc_cache_paths.ohlc_cache_path",
        lambda e, market="": cache_file.with_suffix(".jsonl"),
    )

    assert write_binary_ohlc_cache(epic, high=high, low=low, close=close, spread=spreads) is True
    assert cache_file.is_file()

    t0 = time.perf_counter()
    h2, l2, c2, s2, count = load_binary_ohlc_cache(epic)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert count == n
    assert elapsed_ms < 100.0
    np.testing.assert_allclose(h2[:n], high)
    np.testing.assert_allclose(c2[:n], close)


def test_adaptive_spread_fuse_triggers_freeze_and_route_block():
    from runtime.master_orchestrator import freeze_epic_entries, resolve_execution_route
    from runtime.portfolio_exploration_engine import (
        evaluate_adaptive_spread_fuse,
        is_spread_fuse_frozen,
        record_spread_fuse_sample,
    )

    epic = "CS.D.CFPGOLD.CFP.IP"
    now = time.time()
    for i in range(12):
        record_spread_fuse_sample(epic, 0.30 + i * 0.01)
        time.sleep(0.001)
    row = evaluate_adaptive_spread_fuse(epic, 5.50)
    assert row.get("frozen") is True
    assert is_spread_fuse_frozen(epic) is True
    freeze_epic_entries(epic, reason="adaptive_spread_fuse")
    decision = resolve_execution_route(epic)
    assert decision.allow_entry is False
    assert decision.execution_path == "frozen"
    assert "spread" in decision.reason.lower()


def test_regime_kalman_suppresses_flicker():
    from runtime.portfolio_exploration_engine import get_regime_kalman_snapshot, smooth_regime_with_kalman

    epic = "IX.D.NIKKEI.IFM.IP"
    states = [0, 2, 0, 2, 0, 1, 1, 1]
    for st in states:
        smooth_regime_with_kalman(epic, st, 0.72)
    snap = get_regime_kalman_snapshot()
    row = snap["epics"][epic]
    assert row["smoothed_state"] in (0, 1, 2)
    assert 0.0 <= row["confidence"] <= 1.0


def test_multi_horizon_curves_structure():
    from trading.probability_engine import compute_multi_horizon_curves

    epic = "CS.D.EURUSD.CFD.IP"
    vector = np.zeros(128, dtype=np.float64)
    vector[0] = 0.62
    vector[5] = 0.55
    vector[6] = 0.45
    payload = {"vector": vector.tolist(), "ts_ms": int(time.time() * 1000), "dim": 128}
    curves = compute_multi_horizon_curves(epic=epic, direction="BUY", feature_payload=payload)
    assert "5_tick" in curves["curves"]
    assert "15_min" in curves["curves"]
    assert "4_hour" in curves["curves"]
    assert "win_prob" in curves["curves"]["5_tick"]


def test_multi_horizon_veto_blocks_conflicting_hf_scalp(monkeypatch):
    from trading.probability_engine import (
        STRATEGY_THRESHOLD_LOW_PCT,
        apply_hierarchical_probability_gate,
    )

    epic = "CS.D.EURUSD.CFD.IP"
    vector = np.zeros(128, dtype=np.float64)
    payload = {"vector": vector.tolist(), "ts_ms": int(time.time() * 1000), "dim": 128}

    monkeypatch.setattr(
        "trading.probability_engine.cross_horizon_veto_limit_chase",
        lambda **kw: (True, "multi_horizon_conflict micro=0.50 macro=-0.40"),
    )

    sig = SignalResult(
        signal="BUY",
        raw_confidence=55.0,
        adjusted_confidence=55.0,
        learning_delta=0.0,
        setup_key="test",
        notes="",
        snapshot={"raw_signal": "BUY"},
    )
    verdict = apply_hierarchical_probability_gate(
        sig=sig,
        feature_payload=payload,
        peak_score=max(STRATEGY_THRESHOLD_LOW_PCT + 5, 50.0),
        threshold=50.0,
        epic=epic,
        execution_path="limit_chase_hf",
    )
    assert verdict.veto is True
    assert verdict.model_verdict == "MULTI_HORIZON_VETO"


def test_session_hot_swap_virtual_loop_and_restore(monkeypatch):
    from system.autonomic_healer import SessionHotSwapManager, get_hot_swap_snapshot

    calls: list[str] = []

    def _fake_injector(**kwargs):
        calls.append("inject")

    monkeypatch.setattr(
        "system.market_data_hub.run_synthetic_tick_injector",
        lambda **kw: _fake_injector(**kw),
    )
    monkeypatch.setattr(
        "system.market_data_hub.night_matrix_fresh_count",
        lambda **kw: (4, 4),
    )

    SessionHotSwapManager.on_carrier_drop("carrier_network_drop_test")
    snap = get_hot_swap_snapshot()
    assert snap["virtual_loop_active"] is True
    assert snap["emulation_ticks"] >= 1
    assert len(calls) >= 1

    SessionHotSwapManager.tick()
    SessionHotSwapManager.on_live_handshake_complete()
    restored = get_hot_swap_snapshot()
    assert restored["live_tracking"] is True
    assert restored["virtual_loop_active"] is False


def test_iron_ledger_institutional_matrix_surfaces_in_ai_diagnostics(monkeypatch):
    import runtime.master_orchestrator as mo
    import system.chaos_guardian as cg
    from runtime.institutional_snapshot import build_institutional_matrix_snapshot
    from runtime.portfolio_exploration_engine import record_spread_fuse_sample
    from system.autonomic_healer import get_ai_diagnostics_snapshot

    epic = "CS.D.EURUSD.CFD.IP"
    for _ in range(8):
        record_spread_fuse_sample(epic, 0.00012)

    inst = build_institutional_matrix_snapshot()
    assert inst.get("ok") is True
    assert "spread_fuses" in inst
    assert "zero_copy_pipeline" in inst

    cg.IronLedgerSnapshot.commit(
        {
            "ts": time.time(),
            "orchestrator": {"ok": True},
            "guardian": {"ok": True},
            "institutional": inst,
        }
    )
    monkeypatch.setattr(mo, "publish_iron_ledger_snapshot", lambda: cg.IronLedgerSnapshot.version())

    diag = get_ai_diagnostics_snapshot()
    assert diag.get("institutional", {}).get("ok") is True
    assert "spread_fuses" in diag.get("institutional", {})
    assert "multi_horizon" in diag.get("institutional", {})
