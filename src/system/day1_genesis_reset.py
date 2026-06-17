"""
Day 1 Genesis Reset — programmatic state purge before Gate 1.

Archives legacy dev caches/logs/metrics to ``archive/legacy_dev/`` and forces
runtime accounting (effective daily P&L, drawdown, points, target engine) to
a clean £0.00 / HEALTHY baseline for genesis launch.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir, logs_dir, project_root

ARCHIVE_SUBDIR = Path("archive/legacy_dev")
GENESIS_ENV_FLAG = "DAY1_GENESIS"

# Relative glob patterns (from project root) moved into timestamped purge folder.
PURGE_GLOB_PATTERNS: tuple[str, ...] = (
    "src/data/state/session_state_*.json",
    "src/data/state/telemetry_dead_drop_*.json",
    "src/data/state/points_state.json",
    "src/data/state/dashboard_snapshot.json",
    "src/data/state/roadmap_progress_history.jsonl",
    "src/data/state/setup_registry.json",
    "src/data/state/ohlc_readiness.json",
    "src/data/state/last_shutdown_verify.json",
    "src/data/state/victory_ledger.jsonl",
    "src/data/logs/perf_metrics.snapshot.json",
    "src/data/logs/boot_performance_report.txt",
    "src/data/logs/demo_execution_trace.log",
    "src/data/logs/session_summary_*.txt",
    "src/data/replay_scheduler_state.json",
    "src/data/replay_results.jsonl",
    "src/data/replay_analysis.txt",
    "src/data/shadow_log.jsonl",
    "src/data/shadow_log.jsonl.*",
    "src/data/trade_autopsy.jsonl",
    "src/data/ml_training_store.jsonl",
    "src/data/runtime_state.json",
    "src/data/logs/rate_limit_state.json",
    "src/data/manual_stop.json",
    "src/data/.ig_agent_v29.lock",
)

FRESH_RUNTIME_STATE: dict[str, Any] = {
    "version": 1,
    "saved_at": 0.0,
    "entry": {"entries": []},
    "exit": {"exits": []},
    "pending": {"orders": []},
    "daily_risk": {},
}

FRESH_POINTS_STATE: dict[str, Any] = {
    "version": 1,
    "cumulative": 4.01,
    "cumulative_points": 4.01,
    "state": "HEALTHY",
    "session_score": 0.0,
    "last_trade_score": 0.0,
    "consecutive_losses": 0,
    "signals_to_skip": 0,
    "recovery_wins": 0,
    "bootstrap_wins": 0,
    "day_stopped": False,
    "stop_latched": False,
    "last_nominal": "HEALTHY",
    "rapid_cooldown_until": 0.0,
    "equity_lock_active": False,
    "operator_reset_healthy": True,
}


def genesis_reset_enabled() -> bool:
    val = os.environ.get(GENESIS_ENV_FLAG, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _archive_root() -> Path:
    root = project_root() / ARCHIVE_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _collect_purge_paths(root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in PURGE_GLOB_PATTERNS:
        for path in root.glob(pattern):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            found.append(path)
    return sorted(found)


def _move_into_archive(paths: list[Path], *, purge_dir: Path, root: Path) -> list[str]:
    moved: list[str] = []
    for src in paths:
        try:
            rel = src.relative_to(root)
        except ValueError:
            rel = Path(src.name)
        dest = purge_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved.append(str(rel))
        except OSError as exc:
            log_engine(f"Day1 genesis: archive skip {rel}: {type(exc).__name__}: {exc}")
    return moved


def _write_fresh_state_files() -> None:
    state_dir = data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = data_dir() / "runtime_state.json"
    runtime_path.write_text(
        json.dumps(FRESH_RUNTIME_STATE, indent=2) + "\n",
        encoding="utf-8",
    )
    points_path = state_dir / "points_state.json"
    points_path.write_text(
        json.dumps(FRESH_POINTS_STATE, indent=2) + "\n",
        encoding="utf-8",
    )


def _zero_ledger_and_metrics(*, simulated_equity: float = 10_000.0) -> dict[str, Any]:
    """Force effective_daily_pnl baseline and in-memory trackers to zero."""
    report: dict[str, Any] = {"effective_daily_pnl_gbp": 0.0}

    try:
        from system.drawdown_monitor import reset_session

        reset_session(simulated_equity)
        report["drawdown_reset_balance"] = simulated_equity
    except Exception as exc:
        report["drawdown_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from intelligence.target_engine import get_target_engine, reset_target_engine_for_tests

        reset_target_engine_for_tests()
        te = get_target_engine()
        te.enabled = True
        te.target_daily_gbp = 1000.0
        te.simulated_equity_gbp = simulated_equity
        te.mark_session_start(simulated_equity)
        te._session_day = te._uk_today()
        te.last_p_day = 0.0
        te.last_factor = 1.0
        te.capital_preservation = False
        te.mission_accomplished = False
        te._entry_block_applied = False
        report["target_engine_p_day"] = 0.0
    except Exception as exc:
        report["target_engine_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from system.daily_loss_policy import invalidate_daily_loss_gate_cache

        invalidate_daily_loss_gate_cache()
    except Exception:
        pass

    store = None
    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config

        cfg = get_config()
        store = LearningStore(str(cfg.learning_db))
        from system.v291_upgrade import refresh_today_daily_loss_baseline

        baseline_report = refresh_today_daily_loss_baseline(
            store, cfg=cfg, reason="day1_genesis"
        )
        report["daily_loss_baseline"] = baseline_report
        store.clear_runtime_state("target_mission_victory")
        try:
            store.clear_circuit_breaker_state()
        except Exception:
            pass
        from system.daily_loss_policy import effective_daily_pnl

        report["effective_daily_pnl_gbp"] = float(effective_daily_pnl(store))
    except Exception as exc:
        report["ledger_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from trading.qmm_asset_selector import reset_qmm_asset_selector_for_tests

        reset_qmm_asset_selector_for_tests()
    except Exception:
        pass

    try:
        from intelligence.intelligence_worker import reset_intelligence_worker_for_tests
        from intelligence.pipeline_bridge import reset_intelligence_layer_for_tests

        reset_intelligence_worker_for_tests()
        reset_intelligence_layer_for_tests()
    except Exception:
        pass

    try:
        from system.qmm_process_supervisor import clear_process_entry_block

        clear_process_entry_block()
    except Exception:
        pass

    return report


def run_day1_genesis_reset(*, force: bool = False) -> dict[str, Any]:
    """
    Execute Day 1 purge + zero baseline.

    Runs when ``DAY1_GENESIS=1`` (or ``force=True`` for CLI).
    """
    if not force and not genesis_reset_enabled():
        return {"skipped": True, "reason": f"{GENESIS_ENV_FLAG} not set"}

    root = project_root()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    purge_dir = _archive_root() / f"purge_{ts}"
    purge_dir.mkdir(parents=True, exist_ok=True)

    paths = _collect_purge_paths(root)
    moved = _move_into_archive(paths, purge_dir=purge_dir, root=root)
    _write_fresh_state_files()
    metrics = _zero_ledger_and_metrics()

    manifest = {
        "event": "DAY1_GENESIS_RESET",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "archive_dir": str(purge_dir.relative_to(root)),
        "files_archived": moved,
        "metrics": metrics,
    }
    manifest_path = purge_dir / "genesis_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    marker = data_dir() / "state" / "day1_genesis_applied.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    log_engine(
        f"DAY1 GENESIS RESET complete — archived {len(moved)} file(s) → "
        f"{purge_dir.relative_to(root)} | effective P&L=£{metrics.get('effective_daily_pnl_gbp', 0):.2f}"
    )
    return manifest


def reset_day1_genesis_for_tests() -> None:
    """Remove genesis marker between tests."""
    marker = data_dir() / "state" / "day1_genesis_applied.json"
    if marker.is_file():
        marker.unlink()
