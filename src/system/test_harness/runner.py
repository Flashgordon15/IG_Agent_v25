"""
Detached test harness — replay N ticks post-READY, prove mock execution, exit cleanly.

Activated via ``python src/main.py --test-harness-ticks=N``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import guard_call, log_guarded_exception


@dataclass
class HarnessSummary:
    """Structured harness outcome for logs and exit-code decisions."""

    tick_target: int
    ticks_emitted: int = 0
    loop_ticks_run: int = 0
    gates_passed: int = 0
    orders_attempted: int = 0
    open_positions: int = 0
    mock_transactions_ok: bool = False
    mock_snapshot_ok: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.ticks_emitted < self.tick_target:
            return False
        if self.errors:
            return False
        if not self.mock_transactions_ok or not self.mock_snapshot_ok:
            return False
        return True


def configure_harness_env(tick_count: int) -> None:
    """Idempotent harness profile — safe to call on every entry."""
    for key in (
        "IG_APEX_RUNTIME_MODE",
        "IG_TESTBED_ROOT",
        "TESTBED_ALLOW_ZOMBIE",
        "IG_HISTORICAL_REPLAY",
        "IG_REPLAY_SPEED",
        "IG_REPLAY_DILATION",
    ):
        os.environ.pop(key, None)
    os.environ["IG_TEST_HARNESS"] = "1"
    os.environ["IG_TEST_HARNESS_TICKS"] = str(int(tick_count))
    os.environ["IG_HARNESS_SYNC_BOOT"] = "1"
    os.environ["IG_MOCK_FEED"] = "1"
    os.environ["IG_ALLOW_MOCK_TRADING"] = "1"
    os.environ["IG_SESSION_VALIDATION"] = "1"
    os.environ["IG_AGENT_MODE"] = "DEMO"
    os.environ["IG_AGENT_SKIP_DEPLOY_CHECK"] = "1"
    os.environ["IG_AGENT_FROM_LAUNCHER"] = "1"
    os.environ["IG_API_PORT"] = "9199"
    os.environ["NODE_ENV"] = "shadow"
    os.environ["IG_NODE_PROFILE"] = "shadow"
    os.environ.pop("IG_APEX_DESKTOP", None)
    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
    except Exception:
        pass


def _default_archive_path() -> Path:
    from system.paths import project_root

    return project_root() / "src" / "simulation" / "data" / "production_5day_archive.jsonl"


def _load_ticks_limited(path: Path, limit: int) -> list[Any]:
    """Load the first *limit* valid JSONL ticks without parsing the full archive."""
    from simulation.historical_replayer import ReplayTick, _row_to_tick

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
            if tick is not None:
                ticks.append(tick)
    return ticks


def _resolve_mock_rest() -> Any | None:
    try:
        import system.ig_rest_session as session_mod

        with session_mod._lock:
            client = session_mod._client
        if client is not None:
            return client
    except Exception as exc:
        log_guarded_exception("harness_mock_rest", exc)
    return None


def _verify_mock_stubs(rest: Any, summary: HarnessSummary) -> None:
    from ig_api.mock_clients import MockIGRest

    if not isinstance(rest, MockIGRest):
        summary.errors.append("rest client is not MockIGRest — fail-closed")
        return

    try:
        snap = rest.fetch_market_snapshot(
            "CS.D.CFPGOLD.CFP.IP",
            live=False,
            budget_priority="harness",
        )
        if float(snap.get("bid") or 0) <= 0:
            summary.errors.append("fetch_market_snapshot returned invalid bid")
        else:
            summary.mock_snapshot_ok = True
    except Exception as exc:
        summary.errors.append(f"fetch_market_snapshot failed: {type(exc).__name__}: {exc}")

    try:
        txns = rest.fetch_transactions(from_date="2026-01-01", to_date="2026-12-31")
        if not isinstance(txns, list):
            summary.errors.append("fetch_transactions did not return list")
        else:
            summary.mock_transactions_ok = True
    except Exception as exc:
        summary.errors.append(f"fetch_transactions failed: {type(exc).__name__}: {exc}")


def wait_for_ready(*, timeout_sec: float = 600.0) -> bool:
    """Poll SystemState until READY or timeout (fail-closed)."""
    from system.system_state import BootPhase, get_system_state

    poll = 0.01 if os.environ.get("IG_HARNESS_SYNC_BOOT", "").strip() == "1" else 0.05
    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline:
        snap = get_system_state().snapshot_model()
        phase = str(getattr(snap.phase, "value", snap.phase))
        if snap.ready and phase != BootPhase.FAILED.value:
            return True
        if snap.error_gate:
            log_engine(
                f"HARNESS FAIL-CLOSED: boot failed at {snap.error_gate}: {snap.error}"
            )
            return False
        time.sleep(poll)
    log_engine(f"HARNESS FAIL-CLOSED: READY not reached within {timeout_sec:.0f}s")
    return False


def run_sync_harness_boot(boot_context: Any | None = None) -> Any:
    """Synchronous G2→G5 boot — no uvicorn (integrity / sub-3s harness path)."""
    from system.boot.coordinator_factory import create_boot_coordinator

    coord = create_boot_coordinator(context=boot_context)
    if not coord.state.gate_complete("G1"):
        coord.ensure_g1_complete()
    coord.run_pipeline()
    if not coord.state.snapshot_model().ready:
        raise RuntimeError(
            f"harness sync boot failed: {coord.state.snapshot_model().error_gate} "
            f"{coord.state.snapshot_model().error}"
        )
    log_engine("HARNESS: synchronous boot pipeline READY")
    return coord.context


def run_harness_tick_phase(
    tick_count: int,
    *,
    boot_context: Any | None = None,
    archive_path: Path | None = None,
) -> HarnessSummary:
    """
    After boot READY: emit *tick_count* replay ticks and drive orchestrator loops.

    Bypasses live websocket feeds — uses MarketDataHub replay ingest only.
    """
    from simulation.replay_clock import set_replay_time
    from system.market_data_hub import get_market_data_hub
    from system.stream_ready import signal_stream_ready

    summary = HarnessSummary(tick_target=max(1, int(tick_count)))
    path = archive_path if archive_path is not None else _default_archive_path()
    if not path.is_file():
        summary.errors.append(f"replay archive missing: {path}")
        return summary

    selected = _load_ticks_limited(path, summary.tick_target)
    if len(selected) < summary.tick_target:
        summary.errors.append(
            f"archive yielded {len(selected)} ticks, need {summary.tick_target}"
        )
        return summary

    hub = get_market_data_hub()
    signal_stream_ready(source="test_harness_pre_feed")

    try:
        from system.protective_learning import activate_test_mode_runtime

        activate_test_mode_runtime()
    except Exception as exc:
        log_guarded_exception("harness_test_mode", exc)

    try:
        from apex.warmup_progress import mark_warmup_ready

        mark_warmup_ready()
        log_engine("HARNESS: array warmup marked ready (circuit breaker bypass)")
    except Exception as exc:
        log_guarded_exception("harness_warmup_ready", exc)

    orchestrator = getattr(boot_context, "orchestrator", None) if boot_context else None
    loops: list[Any] = list(getattr(orchestrator, "loops", []) or []) if orchestrator else []

    if not loops:
        summary.errors.append("no orchestrator loops available post-READY")
        return summary

    stop_fn = getattr(orchestrator, "stop", None)
    if callable(stop_fn):
        guard_call("harness_orchestrator_stop", stop_fn)

    epic_loops: dict[str, Any] = {}
    for loop in loops:
        epic_key = str(getattr(loop, "_epic", "") or "").strip()
        if epic_key:
            epic_loops[epic_key] = loop

    rest = _resolve_mock_rest()
    if rest is not None:
        _verify_mock_stubs(rest, summary)

    log_engine(
        f"HARNESS: feeding {summary.tick_target} replay ticks "
        f"across {len(epic_loops)} epic loop(s)"
    )

    for tick in selected:
        epoch = float(tick.timestamp)
        set_replay_time(epoch)

        def _publish_tick() -> Any:
            return hub.publish_replay_tick(
                tick.epic,
                float(tick.bid),
                float(tick.offer),
                quote_time=epoch,
            )

        published = guard_call("harness_publish_tick", _publish_tick, epic=tick.epic)
        if published is None:
            continue
        summary.ticks_emitted += 1

        if rest is not None and hasattr(rest, "set_quote"):
            rest.set_quote(float(tick.bid), float(tick.offer))

        loop = epic_loops.get(str(tick.epic))
        if loop is None:
            continue
        run_once = getattr(loop, "run_once", None)
        if not callable(run_once):
            continue
        ctx = guard_call("harness_run_once", run_once, epic=tick.epic)
        if ctx is None:
            continue
        summary.loop_ticks_run += 1
        if getattr(ctx, "all_passed", False):
            summary.gates_passed += 1
            summary.orders_attempted += 1

    if rest is not None:
        try:
            positions = rest.open_positions()
            summary.open_positions = len(positions)
            _verify_mock_stubs(rest, summary)
        except Exception as exc:
            summary.errors.append(f"open_positions failed: {type(exc).__name__}: {exc}")

    return summary


def emit_harness_summary(summary: HarnessSummary) -> None:
    """Log a scannable harness report block."""
    status = "PASS" if summary.ok else "FAIL-CLOSED"
    twin_line = ""
    try:
        from system.ml.twin_engine_core import get_twin_engine_core

        twin = get_twin_engine_core().telemetry_dict()
        twin_line = (
            f"twin_engine hot_swaps={twin.get('hot_swaps', 0)} "
            f"edge={float(twin.get('win_rate_edge') or 0.0):.4f} "
            f"live_model_v={twin.get('live_model_version', 0)}"
        )
    except Exception as exc:
        log_guarded_exception("harness_twin_telemetry", exc)

    lines = [
        "=== TEST HARNESS SUMMARY ===",
        f"status={status}",
        f"ticks_target={summary.tick_target} ticks_emitted={summary.ticks_emitted}",
        f"loop_ticks_run={summary.loop_ticks_run} gates_passed={summary.gates_passed}",
        f"orders_attempted={summary.orders_attempted} open_positions={summary.open_positions}",
        f"mock_snapshot_ok={summary.mock_snapshot_ok} mock_transactions_ok={summary.mock_transactions_ok}",
    ]
    if twin_line:
        lines.append(twin_line)
    lines.append(
        f"timestamp={datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    if summary.errors:
        lines.append("errors=" + "; ".join(summary.errors))
    block = "\n".join(lines)
    log_engine(block)
    print(block, flush=True)
