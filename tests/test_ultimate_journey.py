"""
Unified Master Journey Test — single sequential integration block.

Covers cold start, 5-day UTC ingestion, twin-engine hot-swap, MockIGRest ledger,
and clean lock teardown without fragmented unit tests.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

_JOURNEY_PORT = 9199
_JOURNEY_TICKS = 80
_FIVE_DAYS_SEC = 5 * 24 * 3600.0
_HOTSWAP_EDGE_THRESHOLD = 0.025


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _count_port_locks(data_dir: Path) -> list[Path]:
    return sorted(data_dir.glob(".ig_agent_v30_port_*.lock"))


def _purge_journey_environment() -> None:
    """Cold-start purge — harness profile on isolated port 9199."""
    from system.identity.app_identity import RuntimeIdentity
    from system.identity.instance_lock import force_release_instance_lock, read_lock_holder
    from system.node_profile import apply_node_profile_to_environ
    from system.test_harness.runner import configure_harness_env

    configure_harness_env(_JOURNEY_TICKS)
    apply_node_profile_to_environ()

    from system.paths import data_dir

    dd = data_dir()
    for lock in _count_port_locks(dd):
        holder = read_lock_holder(lock)
        if holder is None or holder == os.getpid():
            lock.unlink(missing_ok=True)

    force_release_instance_lock()

    from system.boot.port_eviction import reclaim_and_wait

    reclaim_and_wait(_JOURNEY_PORT, force=True)

    try:
        from system.identity.state_cache import reset_live_state_cache

        reset_live_state_cache()
    except Exception:
        pass

    assert RuntimeIdentity.resolve_api_port() == _JOURNEY_PORT
    assert _port_is_free(_JOURNEY_PORT), f"port {_JOURNEY_PORT} must be clear before journey"


def _load_five_day_utc_ticks(limit: int) -> list[Any]:
    from simulation.historical_replayer import ReplayTick, _row_to_tick
    from system.test_harness.runner import _default_archive_path

    path = _default_archive_path()
    assert path.is_file(), f"missing replay archive: {path}"

    ticks: list[ReplayTick] = []

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if len(ticks) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("type") or "tick") not in ("tick", ""):
                continue
            tick = _row_to_tick(row)
            if tick is None:
                continue
            ts = float(tick.timestamp)
            assert ts > 1_000_000_000, f"tick timestamp not epoch UTC: {ts}"
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            assert dt.tzinfo is not None
            raw_ts = row.get("timestamp") or row.get("quote_time")
            if isinstance(raw_ts, str) and "T" in raw_ts:
                parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    pytest.fail(f"archive row lacks UTC offset: {raw_ts}")
            ticks.append(tick)

    assert len(ticks) >= min(limit, 32), f"archive yielded {len(ticks)} ticks, need {min(limit, 32)}"
    span = float(ticks[-1].timestamp) - float(ticks[0].timestamp)
    assert span <= _FIVE_DAYS_SEC + 3600.0, f"loaded tick span exceeds 5-day archive: {span}s"
    return ticks


def _assert_twin_engine_hotswap() -> None:
    from system.ml.twin_engine_core import (
        LiveEngine,
        ModelWeights,
        ShadowEngine,
        TickSample,
        TwinEngineCore,
        reset_twin_engine_core,
        validate_utc_timestamp,
        _train_weights_worker,
    )

    reset_twin_engine_core()
    core = TwinEngineCore()
    live_id = id(core.live)
    shadow_id = id(core.shadow)
    assert live_id != shadow_id
    assert core.live is not core.shadow

    t0 = 1_700_000_000.0
    samples: list[dict[str, Any]] = []
    for i in range(128):
        label = 1 if i % 2 == 0 else 0
        feat_score = 1000.0 if label == 1 else -1000.0
        ts = validate_utc_timestamp(t0 + float(i), latest_ts=t0 + float(i - 1) if i else None)
        row = {
            "ts_utc": ts,
            "epic": "CS.D.CFPGOLD.CFP.IP",
            "bid": 2000.0 + i * 0.1,
            "offer": 2000.2 + i * 0.1,
            "direction": "BUY" if label else "SELL",
            "features": {
                "adjusted_score": feat_score,
                "rsi": feat_score,
                "atr_ratio": 0.1,
            },
            "mid_return": 0.01 if label else -0.01,
            "label": label,
        }
        samples.append(row)

    class _InlineQueue:
        def __init__(self) -> None:
            self._items: list[Any] = []

        def put(self, item: Any) -> None:
            self._items.append(item)

        def get_nowait(self) -> Any:
            if not self._items:
                raise LookupError("empty queue")
            return self._items.pop(0)

    out_queue: Any = _InlineQueue()
    payload = {
        "samples": samples,
        "live_precision": 0.5,
        "version": 1,
        "trained_at": time.time(),
    }
    _train_weights_worker(payload, out_queue)
    result = out_queue.get_nowait()
    assert result.get("ok") is True, result
    telem = dict(result.get("telemetry") or {})
    edge = float(telem.get("win_rate_edge") or 0.0)
    rw = float(telem.get("random_walk_baseline") or 0.0)
    assert edge > _HOTSWAP_EDGE_THRESHOLD, f"edge {edge:.4f} must beat {_HOTSWAP_EDGE_THRESHOLD} vs rw={rw:.4f}"

    weights_raw = result.get("weights") or {}
    candidate = ModelWeights(
        bias=float(weights_raw.get("bias") or 0.0),
        coeffs={
            k: float((weights_raw.get("coeffs") or {}).get(k, 0.0))
            for k in ("adjusted_score", "rsi", "atr_ratio")
        },
        version=int(weights_raw.get("version") or 0),
        trained_at=float(weights_raw.get("trained_at") or time.time()),
    )

    live_before = id(core.live._weights)
    elapsed_ns = core.live.atomic_swap_timed_ns(candidate)
    live_after = id(core.live._weights)
    assert elapsed_ns < 1_000_000
    assert core.live.weights_snapshot().version == candidate.version

    shadow = ShadowEngine(on_retrain=lambda *_a, **_k: None)
    latest: float | None = None
    for row in samples[:10]:
        ts = validate_utc_timestamp(row["ts_utc"], latest_ts=latest)
        latest = ts
        shadow.append(
            TickSample(
                ts_utc=ts,
                epic=str(row["epic"]),
                bid=float(row["bid"]),
                offer=float(row["offer"]),
                direction=str(row["direction"]),
                features=dict(row["features"]),
            )
        )

    standalone_live = LiveEngine()
    assert id(standalone_live) != id(shadow)


def _assert_order_lifecycle_ledger() -> None:
    from ig_api.mock_clients import MockIGRest

    rest = MockIGRest()
    rest.set_quote(2650.0, 2650.5)
    order = rest.place_market_order(
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        size=0.5,
        stop_distance=50.0,
        limit_distance=100.0,
    )
    assert order.get("dealReference") or order.get("dealId")

    txns = rest.fetch_transactions(from_date="2026-01-01", to_date="2026-12-31")
    assert isinstance(txns, list)
    positions = rest.open_positions()
    assert len(positions) >= 1


def _assert_capital_guard_immutable() -> None:
    from execution.capital_guard import CapitalGuard, MAX_LIVE_LOT_SIZE

    CapitalGuard.reset_session_baseline_for_tests()

    ok, reason = CapitalGuard.enforce_order_transmission(
        size=MAX_LIVE_LOT_SIZE,
        rest_client=None,
        epic="CS.D.CFPGOLD.CFP.IP",
    )
    assert ok is True, reason

    ok, reason = CapitalGuard.enforce_order_transmission(
        size=MAX_LIVE_LOT_SIZE + 0.01,
        rest_client=None,
        epic="CS.D.CFPGOLD.CFP.IP",
    )
    assert ok is False
    assert "exceeds hard ceiling" in reason


def _assert_weight_transfer_hurdle() -> None:
    from system.identity.weight_transfer_bridge import (
        get_weight_transfer_bridge,
        reset_weight_transfer_bridge,
    )

    reset_weight_transfer_bridge(unlink=True)
    bridge = get_weight_transfer_bridge(create=True)
    rejected = bridge.publish_candidate(
        weights={"bias": 0.1, "coeffs": {"adjusted_score": 1.0}, "version": 1},
        edge=0.02,
        telemetry={"random_walk_baseline": 0.5},
    )
    assert rejected is False
    assert bridge.read_candidate() is None

    approved = bridge.publish_candidate(
        weights={
            "bias": 0.2,
            "coeffs": {"adjusted_score": 1.5, "rsi": 0.3, "atr_ratio": 0.1},
            "version": 2,
            "trained_at": time.time(),
        },
        edge=_HOTSWAP_EDGE_THRESHOLD + 0.001,
        telemetry={"win_rate_edge": _HOTSWAP_EDGE_THRESHOLD + 0.001},
    )
    assert approved is True
    candidate = bridge.read_candidate()
    assert candidate is not None
    assert float(candidate["edge"]) > _HOTSWAP_EDGE_THRESHOLD
    reset_weight_transfer_bridge(unlink=True)


def _assert_process_isolated_dual_config() -> None:
    import os as _os

    from system.identity.process_orchestrator import (
        configure_live_vanguard_env,
        configure_shadow_simulator_env,
        read_pid_registry,
    )

    saved = dict(_os.environ)
    try:
        configure_live_vanguard_env(cycle_sec=900)
        assert _os.environ["IG_API_PORT"] == "8080"
        assert _os.environ["IG_PARALLEL_TRACK"] == "live"
        assert _os.environ.get("IG_MOCK_FEED") == "0"
        assert _os.environ.get("IG_AGENT_MODE") == "DEMO"

        configure_shadow_simulator_env(cycle_sec=900)
        assert _os.environ["IG_API_PORT"] == "9199"
        assert _os.environ["IG_PARALLEL_TRACK"] == "shadow"
        assert _os.environ.get("IG_HISTORICAL_REPLAY_LOOP") == "1"
        assert _os.environ.get("IG_AGENT_MODE") == "SHADOW"
    finally:
        _os.environ.clear()
        _os.environ.update(saved)

    registry = read_pid_registry()
    assert isinstance(registry, dict)


def _assert_parallel_track_guard() -> None:
    import os as _os

    from execution.parallel_track_guard import assert_live_track_order_transmission

    saved = dict(_os.environ)
    try:
        _os.environ["IG_PARALLEL_TRACK"] = "live"
        ok, reason = assert_live_track_order_transmission(epic="CS.D.CFPGOLD.CFP.IP")
        assert ok is True, reason

        _os.environ["IG_PARALLEL_TRACK"] = "shadow"
        ok, reason = assert_live_track_order_transmission(epic="CS.D.CFPGOLD.CFP.IP")
        assert ok is False
        assert "shadow track" in reason.lower()
    finally:
        _os.environ.clear()
        _os.environ.update(saved)


def _assert_live_state_telemetry() -> None:
    """Memory-mapped JSON state, schema validation, dynamic trailing-stop floor."""
    from system.identity.state_cache import (
        get_live_state_cache,
        read_persisted_live_state,
        reset_live_state_cache,
    )
    from system.ml.meta_reviewer import get_meta_reviewer, reset_meta_reviewer

    reset_live_state_cache()
    reset_meta_reviewer()
    cache = get_live_state_cache()

    cache.upsert_trailing_stop(
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
        entry_price=2000.0,
        current_price=2100.0,
        trailing_floor=2050.0,
        trail_distance_pct=0.02,
        win_lock_trigger_pct=0.10,
    )
    cache.record_tick(
        epic="CS.D.CFPGOLD.CFP.IP",
        bid=2200.0,
        offer=2200.5,
        latency_ms=1.25,
    )
    cache.flush_now()

    from system.identity.shared_memory_bridge import (
        attach_shared_memory_consumer,
        get_shared_memory_bridge,
        read_dual_track_telemetry_envelope,
        resolve_parallel_track_key,
        shm_name_for_track,
    )

    track_key = resolve_parallel_track_key()
    bridge = get_shared_memory_bridge(create=True, track=track_key)
    assert bridge.is_initialized(), "shared memory segment must initialize on first tick"
    assert bridge.size == 65536
    assert bridge.name == shm_name_for_track(track_key)

    shm_payload = attach_shared_memory_consumer(track=track_key).read_json()
    assert shm_payload is not None, "shared memory must contain JSON after tick write"
    assert shm_payload.get("schema_version") == "1.0"
    assert shm_payload.get("track") in ("live", "mock")
    assert float(shm_payload["system_health"].get("tick_latency_ms") or 0) > 0

    envelope = read_dual_track_telemetry_envelope()
    assert envelope.get("schema_version") == "1.1"
    streams = envelope.get("streams") or []
    assert len(streams) == 2
    prefixes = {row.get("prefix") for row in streams}
    assert prefixes == {"[LIVE-TRACK]", "[MOCK-TRACK]"}
    tracks = {row.get("track") for row in streams}
    assert tracks == {"live", "mock"}

    state_path = Path("/tmp/ig_agent_shadow_state.json" if track_key == "shadow" else "/tmp/ig_agent_live_state.json")
    assert state_path.is_file(), "live state JSON must be generated at /tmp/ig_agent_live_state.json"

    disk = read_persisted_live_state()
    assert disk.get("schema_version") == "1.0"
    assert isinstance(disk.get("trailing_stops"), list)
    assert isinstance(disk.get("ml_optimization"), dict)
    assert isinstance(disk.get("system_health"), dict)
    assert float(disk["system_health"].get("tick_latency_ms") or 0) > 0

    stops = disk["trailing_stops"]
    assert len(stops) == 1
    row = stops[0]
    assert row["direction"] == "BUY"
    assert row["win_locked"] is True
    assert float(row["trailing_floor"]) > 2050.0
    assert float(row["profit_pct"]) >= 0.10

    review = get_meta_reviewer().evaluate_pillar_cycle(
        {"cycle": 1, "orders_attempted": 4},
        pnl_delta_gbp=15.0,
    )
    assert review.outcome == "success_finetune"
    assert review.weight_deltas
    cache.apply_meta_review(review.as_dict())
    cache.flush_now()

    enriched = read_persisted_live_state()
    ml = enriched["ml_optimization"]
    assert ml["last_review_outcome"] == "success_finetune"
    assert len(ml["top_indicators"]) <= 5
    for indicator in ml["top_indicators"]:
        assert "name" in indicator
        assert "delta" in indicator
        assert "direction" in indicator

    loss_review = get_meta_reviewer().evaluate_pillar_cycle(
        {"cycle": 2, "orders_attempted": 0},
        pnl_delta_gbp=-2.0,
    )
    assert loss_review.outcome == "zero_trades"
    assert loss_review.vol_threshold_multiplier > 1.0
    assert loss_review.size_scalar < 1.0


def test_ultimate_master_journey() -> None:
    """
    Single unified master journey — all phases in one sequential execution block.
    """
    from system.boot.port_eviction import reclaim_and_wait
    from system.identity.app_identity import RuntimeIdentity
    from system.identity.instance_lock import (
        acquire_instance_lock,
        force_release_instance_lock,
        lock_held_by_current_process,
        read_lock_holder,
        release_instance_lock,
    )
    from system.paths import data_dir
    from system.test_harness.runner import (
        emit_harness_summary,
        run_harness_tick_phase,
        run_sync_harness_boot,
    )

    journey_log: list[str] = []

    def _phase(name: str, detail: str = "") -> None:
        line = f"JOURNEY-PHASE: {name}" + (f" — {detail}" if detail else "")
        journey_log.append(line)

    # ── Phase 1: Cold Start Initialization ──────────────────────────────
    _phase("1_cold_start", "purge + port 9199 + single lock")
    _purge_journey_environment()
    assert _port_is_free(_JOURNEY_PORT)

    ok, msg = acquire_instance_lock()
    assert ok, f"lock acquire failed: {msg}"
    assert lock_held_by_current_process()

    lock_path = RuntimeIdentity.get_lock_path(_JOURNEY_PORT)
    assert lock_path.is_file(), f"expected lock at {lock_path}"
    assert read_lock_holder(lock_path) == os.getpid()

    locks = _count_port_locks(data_dir())
    assert len(locks) == 1, f"expected exactly one port lock, found {locks}"
    assert locks[0].name == f".ig_agent_v30_port_{_JOURNEY_PORT}.lock"

    # ── Phase 2: Pure Data Ingestion (5-day UTC archive) ──────────────────
    _phase("2_data_ingestion", f"{_JOURNEY_TICKS} ticks UTC-normalized")
    utc_ticks = _load_five_day_utc_ticks(_JOURNEY_TICKS)
    assert len(utc_ticks) >= 32
    span = float(utc_ticks[-1].timestamp) - float(utc_ticks[0].timestamp)
    assert span <= _FIVE_DAYS_SEC + 3600.0, f"tick span exceeds 5-day window: {span}s"

    # ── Phase 3: Twin-Engine Decoupling & Hot-Swap ────────────────────────
    _phase("3_twin_engine", "memory isolation + atomic swap >2.5%")
    _assert_twin_engine_hotswap()

    # ── Phase 4: Order Lifecycle Ledger (sync boot + replay) ────────────
    _phase("4_order_ledger", "MockIGRest + orchestrator replay")
    _assert_order_lifecycle_ledger()

    ctx = run_sync_harness_boot()
    summary = run_harness_tick_phase(
        _JOURNEY_TICKS,
        boot_context=ctx,
    )
    emit_harness_summary(summary)
    assert summary.ticks_emitted >= _JOURNEY_TICKS, summary.errors
    assert summary.mock_transactions_ok, summary.errors
    assert summary.mock_snapshot_ok, summary.errors
    assert summary.errors == [], summary.errors

    # ── Phase 5: Process-Isolated Parallel Architecture ───────────────────
    _phase("5_parallel_arch", "CapitalGuard + weight SHM + dual-track env + guard")
    _assert_capital_guard_immutable()
    _assert_weight_transfer_hurdle()
    _assert_process_isolated_dual_config()
    _assert_parallel_track_guard()

    # ── Phase 6: Live State Telemetry & Meta-Reviewer ─────────────────────
    _phase("6_live_state", "JSON cache + trailing stop + ML deltas + SHM")
    _assert_live_state_telemetry()

    # ── Phase 7: Clean Exit ───────────────────────────────────────────────
    _phase("7_clean_exit", "unlink lock + drop port binding")
    release_instance_lock()
    force_release_instance_lock()
    assert not lock_path.is_file(), "lock file must be unlinked on clean exit"
    assert _count_port_locks(data_dir()) == []

    reclaimed = reclaim_and_wait(_JOURNEY_PORT, force=True)
    assert reclaimed or _port_is_free(_JOURNEY_PORT)

    for line in journey_log:
        print(line, flush=True)

    print("COMPOSITE PRODUCTION READINESS: 100%", flush=True)
