#!/usr/bin/env python3
"""
IG Agent v29.1 — isolated chaos & stress harness (no live broker).

Vectors:
  A — Tick avalanche: 50ms protect hub under extreme quote publish rate
  B — SQLite WAL race: parallel shadow upserts vs read-only CSV export
  C — REST poll fault injection: 429 / timeout / empty payload recovery

Usage:
  PYTHONPATH=src python3 scripts/chaos_break_test.py
  PYTHONPATH=src python3 scripts/chaos_break_test.py --burst-seconds 10 --quick
"""

from __future__ import annotations

import argparse
import gc
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import TradeRecord
from data.shadow_training_registry import ensure_schema, upsert_ig_import
from execution.execution_engine import ExecutionEngine
from execution.position_protect_hub import (
    register_execution_engine,
    reset_position_protect_hub_for_tests,
    wire_hub_quotes_to_position_protect,
)
from execution.trailing_stop_engine import TrailEval, eval_trailing_stop
from execution.types import ExecutionMode
from ig_api.exceptions import IGAPIError, RateLimitError
from ig_api.rest_poll_backoff import RestPollBackoff
from ig_api.streaming_client import ConnectionState, IGStreamingClient
from system.config import Config
from system.data_exporter import export_shadow_registry_to_csv
from system.market_data_hub import get_market_data_hub
from trading.points_engine import PointsEngine, set_points_state_path_for_tests
from trading.trade_manager import TradeManager

EPICS = (
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.SPTRD.IFE.IP",
    "CS.D.EURUSD.CFD.IP",
    "IX.D.FTSE.DAILY.IP",
)
MARKETS = {
    "IX.D.NIKKEI.IFM.IP": "Japan 225",
    "CS.D.CFPGOLD.CFP.IP": "Gold",
    "IX.D.SPTRD.IFE.IP": "US 500",
    "CS.D.EURUSD.CFD.IP": "EUR/USD",
    "IX.D.FTSE.DAILY.IP": "FTSE 100",
}
TARGET_EVAL_US = 0.42
MAX_EVAL_US_DEGRADED = 50.0
TICKS_PER_SECOND = 1000


@dataclass
class VectorResult:
    name: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def _isolated_cfg() -> Config:
    return Config(
        _data={
            "operating_mode": "DEMO",
            "account_type": "DEMO",
            "dry_run": True,
            "auto_trade_enabled": True,
            "learning_enabled": False,
            "breakeven_enabled": True,
            "breakeven_trigger_points": 30,
            "breakeven_lock_points": 0,
            "breakeven_offset_points": 0,
            "adaptive_trailing_stop_enabled": True,
            "adaptive_trailing_trigger_points": 10,
            "adaptive_trailing_distance_points": 25,
            "trade_size": 1.0,
            "signal_threshold": 55,
            "cooldown_seconds": 0,
            "risk_points": 40,
            "reward_multiple": 2.0,
            "limit_distance_points": 80,
            "stop_distance_points": 40,
            "max_spread": 35,
            "max_spread_points": 35,
            "adaptive_min_trade_size": 0.5,
            "adaptive_max_trade_size": 3.0,
            "adaptive_min_risk_points": 10,
            "adaptive_max_risk_points": 80,
            "currency_code": "GBP",
        }
    )


def _bench_trailing_eval(iterations: int = 50_000) -> float:
    ev = TrailEval("BUY", 100.0, 95.0, 120.0, 110.0, 55.0, 30.0, 5.0)
    start = time.perf_counter()
    hits = 0
    for i in range(iterations):
        px = 110.0 + (i % 3) * 0.1
        if eval_trailing_stop(
            TrailEval(ev.side, ev.entry, ev.stop, ev.target, px, ev.profit, ev.trigger, ev.distance)
        ):
            hits += 1
    elapsed = time.perf_counter() - start
    if hits == 0:
        raise RuntimeError("trailing eval produced no hits")
    return (elapsed / iterations) * 1_000_000


def _build_protect_stack(
    tmp: Path,
) -> tuple[LearningStore, ExecutionEngine, dict[str, TradeManager]]:
    db_path = tmp / "chaos.db"
    points_path = tmp / "points.json"
    set_points_state_path_for_tests(points_path)
    store = LearningStore(str(db_path))
    store.connect()
    cfg = _isolated_cfg()
    points = PointsEngine(store, state_path=points_path)
    engine = ExecutionEngine(
        mode=ExecutionMode.TEST,
        config=cfg,
        store=store,
        points_engine=points,
    )
    managers: dict[str, TradeManager] = {}
    base = 100.0
    for epic in EPICS:
        mgr = TradeManager(cfg, store, skip_ig_synced_exits=True, points_engine=points)
        managers[epic] = mgr
        tid = store.open_trade(
            TradeRecord(
                id=None,
                market=MARKETS[epic],
                epic=epic,
                side="BUY",
                entry=base,
                exit=None,
                size=1.0,
                stop=base - 10.0,
                target=base + 50.0,
                pnl_points=None,
                result=None,
                confidence=90,
                adjusted_confidence=90,
                setup_key="BUY|bull|asia_early",
                dry_run=True,
                deal_reference=f"CHAOS-{epic}",
                notes="chaos harness",
            )
        )
        store.set_v25_entry_meta(
            tid, confidence_band="high", entry_atr=20.0, trail_distance=25.0
        )
        register_execution_engine(epic, engine)
        base += 10.0
    return store, engine, managers


def vector_a_tick_avalanche(*, burst_seconds: float) -> VectorResult:
    """Blast hub publishes; measure trailing math + fast-path latency & memory."""
    issues: list[str] = []
    metrics: dict[str, Any] = {}

    reset_position_protect_hub_for_tests()
    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    baseline_us = _bench_trailing_eval()
    metrics["baseline_eval_us"] = round(baseline_us, 3)

    with tempfile.TemporaryDirectory(prefix="ig_chaos_a_") as td:
        tmp = Path(td)
        store, engine, _managers = _build_protect_stack(tmp)
        fast_eval_count = 0
        fast_latencies_us: list[float] = []
        orig_fast = engine.update_positions_fast

        def counted_fast(tick: Any) -> list[str]:
            nonlocal fast_eval_count
            t0 = time.perf_counter()
            try:
                return orig_fast(tick)
            finally:
                fast_eval_count += 1
                fast_latencies_us.append((time.perf_counter() - t0) * 1_000_000)

        engine.update_positions_fast = counted_fast  # type: ignore[method-assign]
        unsub = wire_hub_quotes_to_position_protect(min_interval=0.05)
        hub = get_market_data_hub()

        rng = random.Random(42)
        published = 0
        deadline = time.perf_counter() + burst_seconds
        mid = {epic: 100.0 + i * 10.0 for i, epic in enumerate(EPICS)}

        while time.perf_counter() < deadline:
            batch_start = time.perf_counter()
            for epic in EPICS:
                mid[epic] += rng.uniform(-0.5, 0.5)
                px = mid[epic]
                hub.publish(epic, px - 0.5, px + 0.5, source="chaos")
                published += 1
            elapsed = time.perf_counter() - batch_start
            target_batch = len(EPICS) / float(TICKS_PER_SECOND)
            if elapsed < target_batch:
                time.sleep(target_batch - elapsed)

        unsub()
        post_us = _bench_trailing_eval()
        metrics["post_burst_eval_us"] = round(post_us, 3)
        metrics["published_ticks"] = published
        metrics["fast_eval_count"] = fast_eval_count
        metrics["publish_rate_per_s"] = round(published / burst_seconds, 1)
        if fast_latencies_us:
            metrics["fast_path_p50_us"] = round(statistics.median(fast_latencies_us), 2)
            metrics["fast_path_p99_us"] = round(
                sorted(fast_latencies_us)[max(0, int(len(fast_latencies_us) * 0.99) - 1)],
                2,
            )
            metrics["fast_path_max_us"] = round(max(fast_latencies_us), 2)

        store.close()
        set_points_state_path_for_tests(None)

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    stats = snap_after.compare_to(snap_before, "lineno")
    mem_delta_kb = sum(s.size_diff for s in stats[:20]) / 1024
    metrics["memory_delta_kb_top20"] = round(mem_delta_kb, 1)

    if baseline_us > MAX_EVAL_US_DEGRADED:
        issues.append(f"baseline eval {baseline_us:.2f}µs exceeds {MAX_EVAL_US_DEGRADED}µs cap")
    if post_us > max(MAX_EVAL_US_DEGRADED, baseline_us * 3):
        issues.append(
            f"post-burst eval degraded: {post_us:.2f}µs vs baseline {baseline_us:.2f}µs"
        )
    if mem_delta_kb > 5120:
        issues.append(f"memory grew {mem_delta_kb:.0f}KB during burst (possible leak)")

    max_theoretical = int(burst_seconds / 0.05) * len(EPICS)
    if fast_eval_count < max_theoretical * 0.5:
        issues.append(
            f"fast-path eval count low: {fast_eval_count} "
            f"(expected ≥{int(max_theoretical * 0.5)} at 50ms throttle)"
        )

    return VectorResult(
        name="Vector A — Tick Avalanche",
        passed=not issues,
        metrics=metrics,
        issues=issues,
    )


def _chaos_writer(db_path: Path, worker_id: int, errors: list[str]) -> int:
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn.cursor())
    written = 0
    for i in range(50):
        ref = f"CHAOS-B-{worker_id:02d}-{i:03d}"
        row = {
            "deal_reference": ref,
            "ig_deal_id": ref,
            "setup_key": "IG|imported",
            "source": "ig_import",
            "market": "Chaos",
            "epic": EPICS[i % len(EPICS)],
            "side": "BUY",
            "entry": 100.0,
            "exit": 101.0,
            "size": 1.0,
            "pnl_points": 1.0,
            "result": "WIN",
            "closed_at": "2026-06-13 12:00:00",
        }
        try:
            if upsert_ig_import(conn, row):
                written += 1
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                errors.append(f"writer {worker_id}: {exc}")
            else:
                raise
    conn.close()
    return written


def vector_b_db_race(*, workers: int = 50) -> VectorResult:
    """Parallel shadow upserts vs concurrent read-only export."""
    issues: list[str] = []
    metrics: dict[str, Any] = {}
    errors: list[str] = []
    lock_errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ig_chaos_b_") as td:
        db_path = Path(td) / "shadow_chaos.db"
        store = LearningStore(str(db_path))
        store.connect()
        ensure_schema(store.conn.cursor())
        store.conn.commit()
        store.close()

        export_ok = 0
        export_errors: list[str] = []
        stop = threading.Event()

        def reader_loop() -> None:
            nonlocal export_ok
            while not stop.is_set():
                try:
                    result = export_shadow_registry_to_csv(db_path=db_path)
                    if result.get("ok"):
                        export_ok += 1
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        export_errors.append(str(exc))
                    else:
                        export_errors.append(str(exc))
                except Exception as exc:
                    export_errors.append(f"{type(exc).__name__}: {exc}")

        reader = threading.Thread(target=reader_loop, name="chaos-export-reader", daemon=True)
        reader.start()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_chaos_writer, db_path, wid, errors) for wid in range(workers)
            ]
            written_total = 0
            for fut in as_completed(futures):
                written_total += fut.result()

        time.sleep(0.05)
        stop.set()
        reader.join(timeout=5.0)

        lock_errors = [e for e in errors + export_errors if "locked" in e.lower()]
        metrics["writers"] = workers
        metrics["rows_written"] = written_total
        metrics["export_cycles"] = export_ok
        metrics["writer_errors"] = len(errors)
        metrics["export_errors"] = len(export_errors)

        final = export_shadow_registry_to_csv(db_path=db_path)
        metrics["final_row_count"] = (final.get("summary") or {}).get("row_count", 0)

    if lock_errors:
        issues.extend(lock_errors[:5])
        if len(lock_errors) > 5:
            issues.append(f"... and {len(lock_errors) - 5} more database locked errors")
    if written_total == 0:
        issues.append("no shadow rows written")
    if export_ok == 0:
        issues.append("export reader never completed a cycle")

    return VectorResult(
        name="Vector B — Database Race Conditions",
        passed=not issues,
        metrics=metrics,
        issues=issues,
    )


def vector_c_network_injection(*, cycles: int = 120) -> VectorResult:
    """Inject 429/timeout/empty payloads into REST poll loop; verify self-heal."""
    issues: list[str] = []
    metrics: dict[str, Any] = {}

    client = IGStreamingClient(
        MagicMock(),
        MagicMock(is_valid=True),
        rest_client=MagicMock(),
        poll_interval_seconds=0.05,
    )
    client._running = True
    client._epics = set(EPICS[:2])
    client._set_state(ConnectionState.CONNECTING)
    backoff = client._poll_backoff
    mgr = MagicMock()
    mgr.is_rest_blocked.return_value = False
    mgr.is_stream_blocked.return_value = False
    mgr.seconds_until_rest_reset.return_value = 0.0
    mgr.seconds_until_stream_reset.return_value = 0.0

    rng = random.Random(99)
    injected = {"429": 0, "timeout": 0, "empty": 0, "ok": 0}
    crashes: list[str] = []

    class EmptyPayload(Exception):
        pass

    def flaky_fetch(*_args: Any, **_kwargs: Any) -> Any:
        roll = rng.random()
        if roll < 0.35:
            injected["429"] += 1
            raise RateLimitError("HTTP 429 Too Many Requests")
        if roll < 0.55:
            injected["timeout"] += 1
            raise TimeoutError("connection timed out")
        if roll < 0.70:
            injected["empty"] += 1
            return None
        injected["ok"] += 1
        snap = MagicMock()
        snap.bid = 100.0
        snap.offer = 100.5
        return snap

    sleeps: list[float] = []

    with patch("ig_api.streaming_client.time.sleep", side_effect=lambda s: sleeps.append(float(s))):
        with patch("system.market_data_hub.get_market_data_hub") as mock_hub_get:
            hub = MagicMock()
            hub.fetch_if_stale = MagicMock(side_effect=flaky_fetch)
            mock_hub_get.return_value = hub
            for tick in range(cycles):
                try:
                    if mgr.is_rest_blocked():
                        continue
                    client._poll_once(tick, time.time())
                    backoff.on_success()
                except RateLimitError as exc:
                    client._handle_retryable_poll_error(exc, backoff, mgr)
                except Exception as exc:
                    if isinstance(exc, EmptyPayload):
                        continue
                    from ig_api.rest_poll_backoff import is_retryable_poll_error

                    if is_retryable_poll_error(exc):
                        client._handle_retryable_poll_error(exc, backoff, mgr)
                    else:
                        client._handle_poll_error(exc, backoff, mgr)

    metrics["cycles"] = cycles
    metrics["injected"] = injected
    metrics["backoff_sleeps"] = len(sleeps)
    metrics["backoff_strike_final"] = backoff.strike
    metrics["client_state"] = client.state.value
    metrics["first_tick_received"] = client._first_tick_received
    metrics["still_running"] = client._running

    if not client._running:
        issues.append("poll client stopped unexpectedly")
    if client.state == ConnectionState.DISCONNECTED:
        issues.append("client entered DISCONNECTED during fault injection")
    if injected["ok"] == 0:
        issues.append("no successful poll ticks — recovery path never exercised")
    if injected["429"] > 0 and not any(s >= 2.0 for s in sleeps):
        issues.append("429 injected but backoff sleep never engaged")
    if crashes:
        issues.extend(crashes)

    return VectorResult(
        name="Vector C — Network Error Injection",
        passed=not issues,
        metrics=metrics,
        issues=issues,
    )


def _print_vector(result: VectorResult) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"\n{'=' * 60}")
    print(f"[{mark}] {result.name}")
    print("-" * 60)
    for key, val in sorted(result.metrics.items()):
        print(f"  {key}: {val}")
    if result.issues:
        print("  issues:")
        for issue in result.issues:
            print(f"    - {issue}")


def main() -> int:
    parser = argparse.ArgumentParser(description="IG Agent v29.1 chaos harness")
    parser.add_argument(
        "--burst-seconds",
        type=float,
        default=60.0,
        help="Vector A publish burst duration (default 60)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Short burst (10s) and fewer poll cycles",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=50,
        help="Vector B parallel writer count",
    )
    args = parser.parse_args()

    burst = 10.0 if args.quick else args.burst_seconds
    poll_cycles = 40 if args.quick else 120

    print("IG Agent v29.1 — CHAOS & STRESS HARNESS (isolated)")
    print(f"burst={burst}s workers={args.workers} poll_cycles={poll_cycles}")

    results = [
        vector_a_tick_avalanche(burst_seconds=burst),
        vector_b_db_race(workers=args.workers),
        vector_c_network_injection(cycles=poll_cycles),
    ]

    passed = sum(1 for r in results if r.passed)
    for r in results:
        _print_vector(r)

    print(f"\n{'=' * 60}")
    status = "BULLETPROOF" if passed == len(results) else "ISSUES FOUND"
    print(f"CHAOS SUMMARY: {passed}/{len(results)} vectors passed — {status}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
