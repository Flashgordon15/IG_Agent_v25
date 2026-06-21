"""
Native detached daemon cycle kernel — monotonic 15-minute trading + ML heartbeat.

Activated via ``python src/main.py --daemon-cycle=900``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import guard_call, log_guarded_exception

_DEFAULT_LOG_PATH = Path("/tmp/ig_agent.live.log")
_DAEMON_CYCLE_ENV = "IG_DAEMON_CYCLE_SEC"


def configure_daemon_cycle_env(cycle_sec: int) -> None:
    """Legacy single-process daemon — delegates to immutable BootProfile (shadow track)."""
    from system.identity.boot_profile import BootProfile, apply_boot_profile

    sec = max(1, int(cycle_sec))
    apply_boot_profile(BootProfile.for_shadow(cycle_sec=sec))
    os.environ[_DAEMON_CYCLE_ENV] = str(sec)
    os.environ["IG_SESSION_VALIDATION"] = "1"
    os.environ["IG_AGENT_FROM_LAUNCHER"] = "1"
    os.environ.pop("IG_APEX_DESKTOP", None)
    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
    except Exception as exc:
        log_guarded_exception("daemon_cycle_clear_manual_stop", exc)


def is_daemon_cycle_mode() -> bool:
    raw = os.environ.get(_DAEMON_CYCLE_ENV, "").strip()
    return raw.isdigit() and int(raw) > 0


def daemon_cycle_interval_sec() -> float:
    raw = os.environ.get(_DAEMON_CYCLE_ENV, "900").strip()
    try:
        return float(max(1, int(raw)))
    except ValueError:
        return 900.0


def detach_daemon_runtime(*, log_path: Path | None = None) -> None:
    """
    Classic double-fork daemonization.

    The original shell parent exits immediately (clean prompt). The grandchild
    continues with stdout/stderr redirected to *log_path*.
    """
    target = log_path if log_path is not None else _DEFAULT_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    sys.stdout.flush()
    sys.stderr.flush()
    log_fd = open(target, "a", encoding="utf-8", buffering=1)
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    if sys.stdin.isatty():
        with open(os.devnull, "r", encoding="utf-8") as devnull:
            os.dup2(devnull.fileno(), sys.stdin.fileno())

    log_engine(
        f"DAEMON-CYCLE: detached grandchild pid={os.getpid()} "
        f"log={target} interval={daemon_cycle_interval_sec():.0f}s"
    )


def execute_trading_ml_cycle(*, boot_context: Any | None, cycle_num: int) -> dict[str, Any]:
    """
    One daemon heartbeat — drive orchestrator loops + twin-engine overlay.

    Returns a compact telemetry dict for structured logging.
    """
    try:
        from system.identity.instance_lock import acquire_instance_lock

        acquire_instance_lock()
    except Exception as exc:
        log_guarded_exception("daemon_cycle_lock_refresh", exc)

    stats: dict[str, Any] = {
        "cycle": cycle_num,
        "loops_run": 0,
        "gates_passed": 0,
        "orders_attempted": 0,
        "twin_hot_swaps": 0,
        "twin_edge": 0.0,
    }

    orchestrator = getattr(boot_context, "orchestrator", None) if boot_context else None
    loops: list[Any] = list(getattr(orchestrator, "loops", []) or []) if orchestrator else []

    unified_thread_b = False
    try:
        from system.ipc.ring_buffer import unified_engine_active

        unified_thread_b = bool(unified_engine_active())
    except Exception:
        pass

    if unified_thread_b:
        stats["unified_thread_b"] = True
    else:
        for loop in loops:
            run_once = getattr(loop, "run_once", None)
            if not callable(run_once):
                continue
            ctx = guard_call("daemon_cycle_run_once", run_once)
            if ctx is None:
                continue
            stats["loops_run"] += 1
            if getattr(ctx, "all_passed", False):
                stats["gates_passed"] += 1
                stats["orders_attempted"] += 1

    try:
        from system.ml.twin_engine_core import get_twin_engine_core

        twin = get_twin_engine_core().telemetry_dict()
        stats["twin_hot_swaps"] = int(twin.get("hot_swaps") or 0)
        stats["twin_edge"] = float(twin.get("win_rate_edge") or 0.0)
    except Exception as exc:
        log_guarded_exception("daemon_cycle_twin_telemetry", exc)

    try:
        import system.ig_rest_session as session_mod

        with session_mod._lock:
            rest = session_mod._client
        if rest is not None and hasattr(rest, "fetch_transactions"):
            txns = rest.fetch_transactions(from_date="2026-01-01", to_date="2026-12-31")
            stats["ledger_rows"] = len(txns) if isinstance(txns, list) else 0
    except Exception as exc:
        log_guarded_exception("daemon_cycle_ledger_probe", exc)

    pnl_delta = float(stats.get("pnl_delta_gbp") or 0.0)
    track = os.environ.get("IG_PARALLEL_TRACK", "").strip()
    if track in ("live", "unified"):
        try:
            from system.identity.process_orchestrator import apply_live_weight_transfer_if_approved

            if apply_live_weight_transfer_if_approved():
                stats["weight_transfer_applied"] = True
        except Exception as exc:
            log_guarded_exception("daemon_cycle_weight_transfer", exc)
        try:
            from system.identity.live_tolerance_bridge import apply_live_tolerance_if_pending

            if apply_live_tolerance_if_pending():
                stats["live_tolerance_applied"] = True
        except Exception as exc:
            log_guarded_exception("daemon_cycle_live_tolerance", exc)
    try:
        from system.ml.meta_reviewer import get_meta_reviewer

        review = get_meta_reviewer().evaluate_pillar_cycle(stats, pnl_delta_gbp=pnl_delta)
        stats["meta_review_outcome"] = review.outcome
        stats["meta_risk_scalar"] = review.risk_scalar
    except Exception as exc:
        log_guarded_exception("daemon_cycle_meta_reviewer", exc)

    try:
        from system.identity.app_identity import RuntimeIdentity
        from system.identity.state_cache import get_live_state_cache

        port_raw = os.environ.get("IG_API_PORT", "").strip()
        port = int(port_raw) if port_raw.isdigit() else RuntimeIdentity.resolve_api_port()
        cache = get_live_state_cache()
        cache.refresh_system_health(
            api_port=port,
            port_listening=True,
            daemon_pid=os.getpid(),
        )
        cache.flush_now()
    except Exception as exc:
        log_guarded_exception("daemon_cycle_state_cache", exc)

    log_engine(
        "DAEMON-CYCLE: heartbeat complete "
        f"#{cycle_num} loops={stats['loops_run']} gates={stats['gates_passed']} "
        f"orders={stats['orders_attempted']} twin_swaps={stats['twin_hot_swaps']} "
        f"twin_edge={stats['twin_edge']:.4f}"
    )
    return stats


def run_monotonic_cycle_loop(
    *,
    interval_sec: float,
    boot_context: Any | None,
    shutdown_event: threading.Event,
) -> None:
    """
    Drift-free monotonic scheduler — sleeps between heartbeats, never drops state.

    Uses a fixed anchor ``next_deadline += interval_sec`` so cumulative drift
    does not accumulate across cycles.
    """
    interval = float(max(1.0, interval_sec))
    cycle_num = 0
    next_deadline = time.monotonic() + interval

    try:
        from system.identity.instance_lock import acquire_instance_lock

        ok, msg = acquire_instance_lock()
        if not ok:
            log_engine(f"DAEMON-CYCLE: lock refresh failed — {msg}")
    except Exception as exc:
        log_guarded_exception("daemon_cycle_lock_arm", exc)

    log_engine(
        f"DAEMON-CYCLE: scheduler armed interval={interval:.0f}s — "
        f"sleeping until first heartbeat"
    )

    try:
        from system.identity.app_identity import RuntimeIdentity
        from system.identity.state_cache import get_live_state_cache

        cache = get_live_state_cache()
        port_raw = os.environ.get("IG_API_PORT", "").strip()
        port = int(port_raw) if port_raw.isdigit() else RuntimeIdentity.resolve_api_port()
        cache.refresh_system_health(
            api_port=port,
            port_listening=True,
            daemon_pid=os.getpid(),
        )
        cache.flush_now()
    except Exception as exc:
        log_guarded_exception("daemon_cycle_state_cache_boot", exc)

    while not shutdown_event.is_set():
        now = time.monotonic()
        remaining = next_deadline - now
        if remaining > 0:
            shutdown_event.wait(timeout=min(1.0, remaining))
            continue

        cycle_num += 1
        log_engine(f"DAEMON-CYCLE: heartbeat #{cycle_num} firing (interval={interval:.0f}s)")
        execute_trading_ml_cycle(boot_context=boot_context, cycle_num=cycle_num)
        next_deadline += interval
        if next_deadline <= time.monotonic():
            next_deadline = time.monotonic() + interval
        log_engine(
            f"DAEMON-CYCLE: heartbeat #{cycle_num} done — "
            f"sleeping {interval:.0f}s until next cycle"
        )
