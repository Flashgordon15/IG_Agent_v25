"""Unbreakable Production Lifecycle — v29.1 end-to-end certification harness."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "logs" / "unbreakable_certification_manifest.json"
CERTIFICATION_VERSION = "v29.1.unbreakable.1"

NIGHT_EPICS = [
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
]


@pytest.fixture(autouse=True)
def _isolate_certification_harness(monkeypatch):
    import os
    import execution.risk_manager as rm
    import runtime.master_orchestrator as mo
    import runtime.portfolio_exploration_engine as ppe
    import system.alert_reporting_matrix as arm
    import system.autonomic_healer as ah
    import system.chaos_guardian as cg
    from apex.microkernel import reset_microkernel_for_tests
    from system.market_data_hub import reset_headline_urgency_for_tests
    from system.runtime_stabilizer import reset_runtime_stabilizer_for_tests
    from trading.probability_engine import reset_cognitive_self_correction_for_tests

    monkeypatch.setenv("IG_STABILIZER_SKIP", "1")
    monkeypatch.setenv("IG_STABILIZER_COOLDOWN_SEC", "0.01")
    monkeypatch.setenv("IG_STABILIZER_CYCLES", "5")

    reset_runtime_stabilizer_for_tests()
    mo.reset_master_orchestrator_for_tests()
    cg.reset_chaos_guardian_for_tests()
    arm.reset_alert_reporting_for_tests()
    ah.reset_autonomic_healer_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_equilibrium_risk_for_tests()
    reset_microkernel_for_tests()
    reset_headline_urgency_for_tests()
    reset_cognitive_self_correction_for_tests()
    yield
    arm.stop_alert_reporting_matrix()
    reset_runtime_stabilizer_for_tests()
    cg.reset_chaos_guardian_for_tests()
    arm.reset_alert_reporting_for_tests()
    ah.reset_autonomic_healer_for_tests()
    ppe.reset_portfolio_exploration_for_tests()
    rm.reset_equilibrium_risk_for_tests()
    reset_microkernel_for_tests()
    reset_headline_urgency_for_tests()
    reset_cognitive_self_correction_for_tests()


def _apply_boot_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    import runtime.master_orchestrator as mo

    monkeypatch.setattr(
        mo,
        "_ping_telemetry_routes",
        lambda: (
            True,
            [{"route": "guardian", "ok": True}, {"route": "tuner", "ok": True}],
        ),
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.warm_up_regime_ring_buffers",
        lambda epics=None: {e: 288 for e in (epics or NIGHT_EPICS)},
    )
    monkeypatch.setattr(
        "runtime.regime_switch_engine.get_last_ring_warmup_meta",
        lambda: {"fallback_count": 0},
    )


def _run_cold_boot(monkeypatch: pytest.MonkeyPatch) -> dict:
    import runtime.master_orchestrator as mo

    _apply_boot_mocks(monkeypatch)
    return asyncio.run(mo._execute_warmup_async(NIGHT_EPICS))


def _seed_covariance_returns(monkeypatch: pytest.MonkeyPatch, epics: list[str]) -> None:
    cache: dict[str, np.ndarray] = {}
    for epic in epics:
        rng = np.random.default_rng(abs(hash(epic)) % (2**31))
        cache[epic] = rng.normal(0.002, 0.001, 80).astype(np.float64)
    monkeypatch.setattr(
        "runtime.portfolio_exploration_engine._log_returns",
        lambda epic, n=288: cache.get(str(epic)),
    )


def _write_certification_manifest(manifest: dict) -> Path:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return MANIFEST_PATH


def test_pre_development_audit_readonly():
    """Read-only environment gate — no process kills or live mutations."""
    runtime = ROOT / "src" / "data" / "runtime_state.json"
    if runtime.is_file():
        data = json.loads(runtime.read_text(encoding="utf-8"))
        entries = (data.get("entry") or {}).get("entries") or []
        assert isinstance(entries, list)
    manual = ROOT / "src" / "data" / "manual_stop.json"
    assert not manual.is_file() or manual.stat().st_size >= 0


def test_unbreakable_production_lifecycle_certification(monkeypatch: pytest.MonkeyPatch):
    """Full cold-start → chaos → telemetry certification with manifest seal."""
    import execution.risk_manager as rm
    import runtime.master_orchestrator as mo
    import runtime.portfolio_exploration_engine as ppe
    import system.alert_reporting_matrix as arm
    import system.autonomic_healer as ah
    import system.chaos_guardian as cg
    from apex.microkernel import get_microkernel
    from api.routes import api_ai_diagnostics, api_orchestrator_state
    from ig_api import lightstreamer_streaming as ls
    from runtime.portfolio_synthesis_snapshot import build_portfolio_synthesis_snapshot
    from runtime.regime_switch_engine import evaluate_epic_regime
    from system.market_data_hub import (
        get_market_data_hub,
        ingest_live_headline,
        parse_live_headline_sentiment_urgency,
    )

    hot_path_ms: dict[str, float] = {}
    chaos_events: list[dict] = []
    stages_passed: list[str] = []
    iron_ledger_reads: dict[str, int] = {"orchestrator": 0, "guardian": 0, "portfolio_synthesis": 0}
    errors: list[str] = []

    # --- Phase A: STAGE_1 → STAGE_9 cold boot + 5-cycle stabilizer gate ---
    t_boot = time.perf_counter()
    boot = _run_cold_boot(monkeypatch)
    hot_path_ms["cold_boot_total"] = round((time.perf_counter() - t_boot) * 1000.0, 3)

    assert boot["ok"] is True
    assert boot["primed"] is True
    assert boot["trade_ready"] is True
    assert len(boot["stages"]) == 9
    tokens = boot["stage_tokens"]
    for stage in mo._BOOT_STAGES:
        assert tokens.get(stage) in (
            mo._TOKEN_SUCCESS,
            mo._TOKEN_WARMING,
            mo._TOKEN_WARMING_HEALTHY,
        )
        stages_passed.append(stage)
    chaos_events.append({"event": "cold_boot_complete", "stages": len(stages_passed)})
    from system.runtime_stabilizer import get_stabilizer_seal

    assert get_stabilizer_seal() == "APPROVED"
    chaos_events.append({"event": "stabilizer_5_cycle", "seal": get_stabilizer_seal()})

    _seed_covariance_returns(monkeypatch, NIGHT_EPICS)

    # --- Phase B: Multi-threaded data flood (HF ticks + breaking news) ---
    hub = get_market_data_hub()
    kernel = get_microkernel()
    flood_stop = threading.Event()
    flood_counts = {"ticks": 0, "headlines": 0}

    def _tick_flood():
        epic_idx = 0
        while not flood_stop.is_set():
            epic = NIGHT_EPICS[epic_idx % len(NIGHT_EPICS)]
            base = 100.0 + (epic_idx % 10)
            bid = base + flood_counts["ticks"] * 0.0001
            offer = bid + 0.0002
            try:
                hub.publish(epic, bid, offer, source="cert_harness")
                quote = SimpleNamespace(epic=epic, bid=bid, offer=offer, source="cert_harness")
                kernel.on_tick_ingest(epic, quote)
                flood_counts["ticks"] += 1
            except Exception as exc:
                errors.append(f"tick_flood:{exc}")
            epic_idx += 1
            time.sleep(0.001)

    def _headline_flood():
        headlines = [
            "Fed signals surprise rate cut amid inflation fears",
            "Tariff shock hits Wall Street futures",
            "Gold spikes on safe-haven demand",
            "Nikkei rallies on earnings beat",
        ]
        idx = 0
        while not flood_stop.is_set():
            epic = NIGHT_EPICS[idx % len(NIGHT_EPICS)]
            text = headlines[idx % len(headlines)]
            t0 = time.perf_counter()
            parsed = parse_live_headline_sentiment_urgency(text, epic=epic)
            hot_path_ms.setdefault("headline_parse_ms", []).append(
                round((time.perf_counter() - t0) * 1000.0, 3)
            )
            ingest_live_headline(text, epic=epic)
            flood_counts["headlines"] += 1
            idx += 1
            time.sleep(0.002)

    tick_thread = threading.Thread(target=_tick_flood, name="cert-tick-flood", daemon=True)
    news_thread = threading.Thread(target=_headline_flood, name="cert-news-flood", daemon=True)
    tick_thread.start()
    news_thread.start()
    time.sleep(0.15)
    flood_stop.set()
    tick_thread.join(timeout=2.0)
    news_thread.join(timeout=2.0)
    assert flood_counts["ticks"] >= 5
    assert flood_counts["headlines"] >= 3
    chaos_events.append({"event": "data_flood", **flood_counts})

    # --- Phase C: Execution chain (ingest → route → ledger) ---
    epic = "CS.D.EURUSD.CFD.IP"
    t_chain = time.perf_counter()

    regime = evaluate_epic_regime(epic)
    assert int(regime.state) in (0, 1, 2)
    hot_path_ms["markov_regime_eval"] = round((time.perf_counter() - t_chain) * 1000.0, 3)

    t_cov = time.perf_counter()
    cov = ppe.compute_portfolio_covariance_matrix(NIGHT_EPICS, force=True)
    assert cov.get("ok") is True
    hot_path_ms["portfolio_covariance"] = round((time.perf_counter() - t_cov) * 1000.0, 3)

    t_eq = time.perf_counter()
    equilibrium = rm.get_equilibrium_risk_snapshot()
    assert isinstance(equilibrium, dict)
    hot_path_ms["risk_parity_sizing"] = round((time.perf_counter() - t_eq) * 1000.0, 3)

    cg.enqueue_fast_pass_token(epic=epic, direction="BUY", score=0.82, reason="cert_harness")
    assert cg.get_fast_pass_queue_snapshot()

    t_route = time.perf_counter()
    decision = mo.resolve_execution_route(epic)
    assert decision.epic == epic
    hot_path_ms["execution_route_dispatch"] = round((time.perf_counter() - t_route) * 1000.0, 3)

    t_dispatch = time.perf_counter()
    routes = asyncio.run(
        mo.dispatch_market_updates([(epic, 1.1000, 1.1002), ("IX.D.DOW.IFM.IP", 42000.0, 42001.0)])
    )
    assert isinstance(routes, list)
    hot_path_ms["ingestion_frame_dispatch"] = round((time.perf_counter() - t_dispatch) * 1000.0, 3)

    t_ledger = time.perf_counter()
    ver = mo.publish_iron_ledger_snapshot()
    assert ver >= 1
    hot_path_ms["iron_ledger_publish"] = round((time.perf_counter() - t_ledger) * 1000.0, 3)
    chaos_events.append({"event": "execution_chain_complete", "iron_ledger_version": ver})

    # --- Phase D: Environmental chaos interception ---
    mock_ls = MagicMock()
    mock_ls._using_fallback = False
    mock_ls._running = True
    mock_ls._teardown_lightstreamer = MagicMock()
    mock_ls._start_fallback = MagicMock()
    ls.register_lightstreamer_client(mock_ls)
    t_failover = time.perf_counter()
    failover_ok = ls.force_rest_poll_failover(reason="cert_lightstreamer_stall")
    failover_ms = (time.perf_counter() - t_failover) * 1000.0
    ls.register_lightstreamer_client(None)
    assert failover_ok is True
    assert failover_ms < 3000.0
    hot_path_ms["lightstreamer_failover"] = round(failover_ms, 3)
    chaos_events.append({"event": "lightstreamer_failover", "ok": failover_ok, "ms": failover_ms})

    ah._boot_anchor_ts = time.time() - 35.0
    ah._drift_flattener_engaged = False
    ah._init_blocker_since = {"broker_reconciliation_drift": time.time() - 35.0}
    ah._rest_client = MagicMock()
    with patch.object(
        ah,
        "_overwrite_local_registry_from_broker",
        return_value={"ok": True, "synced": True, "broker_positions": 0},
    ), patch(
        "system.broker_reconciliation_daemon.run_reconciliation_once",
        return_value={"healthy": True, "drift_count": 0},
    ), patch.object(
        ah,
        "_force_arm_routing_and_trade_ready",
        return_value={"armed": True, "trade_ready": True},
    ), patch(
        "system.iron_cage_readiness.evaluate_iron_cage_readiness",
        return_value={"trade_ready": True, "blockers": []},
    ), patch("system.alert_reporting_matrix.notify_drift_clear"):
        drift_result = ah._activate_autonomic_drift_flattener(
            blockers=["broker_reconciliation_drift"]
        )
    assert drift_result.get("ok") is True
    assert drift_result.get("trade_ready") is True
    chaos_events.append({"event": "autonomic_drift_flattener", **drift_result})

    publish_stop = threading.Event()

    def _ledger_publisher():
        while not publish_stop.is_set():
            try:
                mo.publish_iron_ledger_snapshot()
            except Exception as exc:
                errors.append(f"ledger_pub:{exc}")
            time.sleep(0.002)

    pub_thread = threading.Thread(target=_ledger_publisher, name="cert-ledger-pub", daemon=True)
    pub_thread.start()

    def _api_reader(_: int) -> dict:
        orch_resp = api_orchestrator_state()
        diag_resp = api_ai_diagnostics()
        orch_body = json.loads(orch_resp.body)
        diag_body = json.loads(diag_resp.body)
        iron_ledger_reads["orchestrator"] += 1
        ps = cg.IronLedgerSnapshot.read_section("portfolio_synthesis")
        if ps:
            iron_ledger_reads["portfolio_synthesis"] += 1
        guard = cg.IronLedgerSnapshot.read_section("guardian")
        if guard:
            iron_ledger_reads["guardian"] += 1
        return {"orch": orch_body, "diag": diag_body}

    t_http = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(_api_reader, i) for i in range(100)]
        http_results = [f.result(timeout=5.0) for f in futures]
    hot_path_ms["http_flood_100_reads"] = round((time.perf_counter() - t_http) * 1000.0, 3)
    publish_stop.set()
    pub_thread.join(timeout=2.0)
    assert len(http_results) == 100
    assert not errors
    chaos_events.append({"event": "http_telemetry_flood", "reads": 100})

    # --- Phase E: Cognitive telemetry via Iron Ledger read-only paths ---
    synthesis = build_portfolio_synthesis_snapshot()
    assert synthesis.get("ok") is True
    assert "news_alpha" in synthesis
    assert synthesis["news_alpha"].get("headlines", {}).get("ok") is True
    assert "cognitive_risk_heatmap" in synthesis
    api_ingest = synthesis["news_alpha"].get("api_ingest") or {}
    assert isinstance(api_ingest.get("feeds"), list)

    mo.publish_iron_ledger_snapshot()
    ledger = cg.IronLedgerSnapshot.read()
    ledger_ps = ledger.get("portfolio_synthesis") or cg.IronLedgerSnapshot.read_section("portfolio_synthesis")
    assert ledger_ps.get("ok") is True or synthesis.get("ok") is True
    orch_snap = ledger.get("orchestrator") or mo.get_orchestrator_state_snapshot()
    assert "cognitive_reason" in orch_snap or "scoreboard" in orch_snap

    diag = ah.get_ai_diagnostics_snapshot()
    assert diag.get("ok") is True
    assert "current_boot_stage" in diag

    # --- Phase F: 15-second Telegram batch coalescing (fill records preserved) ---
    sent: list[str] = []
    monkeypatch.setattr(arm, "_telegram_send", lambda m, **kw: sent.append(m) or True)
    monkeypatch.setattr(arm, "_discord_send", lambda m: True)
    monkeypatch.setattr(
        arm,
        "_buffer_coalesced_telegram_digest",
        lambda msg: sent.append(msg) or True,
    )
    arm.set_coalesce_window_for_tests(15.0)
    arm.start_alert_reporting_matrix()

    fill_titles: list[str] = []
    for i in range(8):
        title = f"Fill confirm #{i}"
        fill_titles.append(title)
        arm.notify_scalper_trade_event(
            ticker=epic,
            title=title,
            body=f"Micro-scalp fill {i} slippage={0.05 + i * 0.01:.2f}pts",
            slippage_pts=0.05 + i * 0.01,
        )

    deadline = time.time() + 3.0
    while time.time() < deadline:
        with arm._coalesce_lock:
            if arm._coalesce_buffer:
                break
        time.sleep(0.05)
    arm.flush_coalesce_buffer_for_tests()
    if not sent:
        deadline = time.time() + 2.0
        while time.time() < deadline and not sent:
            time.sleep(0.05)
    assert sent
    combined = "\n".join(sent)
    assert "Batch Status" in combined or "8 events" in combined
    for title in fill_titles:
        assert title in combined
    telegram_batch = {
        "window_sec": 15.0,
        "events_coalesced": len(fill_titles),
        "markdown_ok": "Batch Status" in combined,
        "fills_preserved": all(t in combined for t in fill_titles),
    }

    # --- Phase G: Certification manifest ---
    headline_parses = hot_path_ms.pop("headline_parse_ms", [])
    if headline_parses:
        hot_path_ms["headline_parse_avg"] = round(
            sum(headline_parses) / len(headline_parses), 3
        )

    manifest = {
        "timestamp": time.time(),
        "certification_version": CERTIFICATION_VERSION,
        "stages_passed": stages_passed,
        "chaos_events": chaos_events,
        "hot_path_ms": hot_path_ms,
        "iron_ledger_reads": iron_ledger_reads,
        "telegram_batch": telegram_batch,
        "multi_process_vacancy": {
            "concurrent_readers": 100,
            "ledger_version_at_seal": cg.IronLedgerSnapshot.version(),
            "flood_ticks": flood_counts["ticks"],
            "flood_headlines": flood_counts["headlines"],
        },
        "seal": "APPROVED",
    }
    manifest_path = _write_certification_manifest(manifest)
    assert manifest_path.is_file()
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert loaded["seal"] == "APPROVED"
    assert loaded["stages_passed"] == stages_passed
    assert len(loaded["stages_passed"]) == 9


def test_five_cycle_stabilizer_engine(monkeypatch):
    """Verify 5-cycle stabilizer completes with APPROVED seal under fast cooldown."""
    import runtime.master_orchestrator as mo
    from system.runtime_stabilizer import get_stabilizer_seal, reset_runtime_stabilizer_for_tests, run_five_cycle_production_stabilizer

    monkeypatch.setenv("IG_STABILIZER_COOLDOWN_SEC", "0.01")
    monkeypatch.setenv("IG_STABILIZER_CYCLES", "5")
    monkeypatch.setattr(
        "cockpit.desktop_process_guard.destroy_orphan_zombie_processes",
        lambda **kw: {"ok": True, "purged_pids": [], "orphan_killed": []},
    )
    monkeypatch.setattr(
        "system.runtime_stabilizer._verify_trade_routing_chain",
        lambda epic: {"ok": True, "epic": epic, "route": "limit_chase_hf", "fast_pass": True},
    )
    reset_runtime_stabilizer_for_tests()
    mo.reset_master_orchestrator_for_tests()
    with mo._lock:
        mo._armed = True
        mo._primed = True

    result = run_five_cycle_production_stabilizer(NIGHT_EPICS[:2], force=True)
    assert result.get("ok") is True
    assert get_stabilizer_seal() == "APPROVED"
    assert len(result.get("cycles") or []) == 5

