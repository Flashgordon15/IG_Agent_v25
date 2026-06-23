"""
Unified multi-threaded engine — single master process, zero IPC.

Thread A (Shadow Coprocessor): Racing multi-feed hub + archive bake → ring buffer.
Thread B (Live Execution): IG tick processing + order dispatch via ring lookup.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from system.bare_metal_exec import bare_metal_hot_path_active
from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_THREAD_A: threading.Thread | None = None
_THREAD_B: threading.Thread | None = None
_STOP = threading.Event()
_BOOT_CONTEXT: Any = None
_STATE: dict[str, Any] = {
    "running": False,
    "thread_a_alive": False,
    "thread_b_alive": False,
    "started_at_epoch": 0.0,
    "multi_feed_hub": False,
}


def configure_unified_engine_env(*, cycle_sec: int = 900, api_port: int = 8080) -> None:
    """Single-process unified boot — replaces dual :8080/:9199 orchestration."""
    os.environ["IG_UNIFIED_ENGINE"] = "1"
    os.environ["IG_PARALLEL_DUAL"] = "0"
    os.environ["IG_PARALLEL_TRACK"] = "unified"
    os.environ["IG_API_PORT"] = str(int(api_port))
    os.environ["IG_PREBAKED_ALPHA_MATRIX"] = "1"
    os.environ["IG_BARE_METAL_EXEC"] = "1"
    os.environ["IG_DAEMON_CYCLE_SEC"] = str(max(1, int(cycle_sec)))
    os.environ["IG_AGENT_FROM_LAUNCHER"] = "1"
    os.environ.pop("IG_ORCHESTRATOR_CHILD", None)
    os.environ.setdefault("IG_AGENT_MODE", "DEMO")
    os.environ.setdefault("IG_PRODUCTION_EXECUTION", "0")
    os.environ.setdefault("IG_MOCK_FEED", "0")
    try:
        from system.shutdown_cleanup import clear_manual_stop

        clear_manual_stop()
    except Exception as exc:
        log_guarded_exception("unified_engine_clear_manual_stop", exc)
    try:
        from system.agent_execution_mode import ensure_production_execution_armed_on_boot

        ensure_production_execution_armed_on_boot()
    except Exception as exc:
        log_guarded_exception("unified_engine_production_exec", exc)


def get_boot_context() -> Any:
    return _BOOT_CONTEXT


def _shadow_coprocessor_loop() -> None:
    """Thread A — racing feeds, archive bake, ring-buffer publish, failover watch."""
    from system.bootstrap_phase_barrier import wait_bootstrap_phase_barrier

    if not wait_bootstrap_phase_barrier(role="thread-a", timeout_sec=120.0):
        log_engine("UnifiedEngine Thread-A: bootstrap barrier timeout — withheld")
        return

    from system.ipc.ring_buffer import get_alpha_ring_buffer

    ring = get_alpha_ring_buffer()
    replayer_started = False

    try:
        from system.feeds.multi_feed_hub import start_racing_multi_feed_hub

        start_racing_multi_feed_hub()
        _STATE["multi_feed_hub"] = True
        log_engine("UnifiedEngine Thread-A: Racing Multi-Feeder Hub armed")
    except Exception as exc:
        log_guarded_exception("unified_thread_a_multi_feed", exc)
        try:
            from feeder.yahoo_quote_poller import start_yahoo_quote_poller
            from system.feeds.multi_feed_hub import yahoo_route_bypassed

            if not yahoo_route_bypassed():
                start_yahoo_quote_poller()
                log_engine("UnifiedEngine Thread-A: Yahoo fallback poller started")
            else:
                log_engine(
                    "UnifiedEngine Thread-A: Yahoo fallback suppressed — WS feeds primary"
                )
        except Exception as yexc:
            log_guarded_exception("unified_thread_a_yahoo_fallback", yexc)

    try:
        from simulation.historical_replayer import default_replay_path, start_background_replay
        from system.market_data_hub import get_market_data_hub

        path = default_replay_path()
        hub = get_market_data_hub()
        start_background_replay(
            path,
            speed=float(os.environ.get("IG_REPLAY_SPEED", "20")),
            hub=hub,
            loop=True,
        )
        replayer_started = True
        log_engine(f"UnifiedEngine Thread-A: HistoricalReplayer armed path={path}")
    except Exception as exc:
        log_guarded_exception("unified_thread_a_replay", exc)

    last_calib = 0.0
    while not _STOP.is_set():
        try:
            from system.feeds.multi_feed_hub import feed_hub_telemetry

            tel = feed_hub_telemetry()
            active = len(tel.get("active_feeds") or [])
            if active < 1:
                log_engine("UnifiedEngine Thread-A: feed failover — zero active streams")
        except Exception:
            pass

        try:
            from intelligence.matrix_prebaker import compile_prebaked_alpha_matrix, get_alpha_matrix_segment

            report = compile_prebaked_alpha_matrix(stride=8)
            seg = get_alpha_matrix_segment(create=False)
            try:
                from system.config_loader import get_config

                cfg = get_config()
            except Exception:
                cfg = None
            ring.write_matrix_generation(
                seg.matrix.copy(),
                vector_density=report.cells_populated,
                cfg=cfg,
            )
        except Exception as exc:
            log_guarded_exception("unified_thread_a_compile", exc)

        now = time.monotonic()
        if now - last_calib >= 1.0:
            try:
                from system.config_loader import get_config

                cfg = get_config()
                signal_thr = float(getattr(cfg, "signal_threshold", 52.5) or 52.5)
                atr_mult = float(
                    getattr(cfg, "adaptive_atr_risk_multiple", None)
                    or getattr(cfg, "atr_multiplier", None)
                    or 2.5
                )
            except Exception:
                signal_thr = 52.5
                atr_mult = 2.5
            ring.write_recency_calibration(
                rsi_bias=0.0,
                atr_bias=0.0,
                mom_bias=0.0,
                recency_weight=1.0,
            )
            last_calib = now

        _STOP.wait(30.0 if replayer_started else 60.0)

    try:
        from system.feeds.multi_feed_hub import stop_racing_multi_feed_hub

        stop_racing_multi_feed_hub()
    except Exception:
        pass
    _STATE["thread_a_alive"] = False


def unified_thread_state() -> dict[str, Any]:
    return {
        "a_alive": bool(_THREAD_A and _THREAD_A.is_alive()),
        "b_alive": bool(_THREAD_B and _THREAD_B.is_alive()),
        "master_pid": os.getpid(),
        "multi_feed_hub": bool(_STATE.get("multi_feed_hub")),
        "running": bool(_STATE.get("running")),
    }


def _live_execution_loop() -> None:
    """Thread B — ring-buffer quote ingest → bare-metal alpha dispatch (no API/logging)."""
    global _BOOT_CONTEXT

    from system.bootstrap_phase_barrier import wait_bootstrap_phase_barrier

    if not wait_bootstrap_phase_barrier(role="thread-b", timeout_sec=120.0):
        log_engine("UnifiedEngine Thread-B: bootstrap barrier timeout — withheld")
        return

    _STATE["thread_b_alive"] = True

    from data.models import Quote

    from system.ipc.ring_buffer import get_alpha_ring_buffer

    ring = get_alpha_ring_buffer()
    last_quote_seq: dict[str, int] = {}
    spin_sec = max(0.0005, float(os.environ.get("IG_UNIFIED_TICK_SEC", "0.001")))

    while not _STOP.is_set():
        orchestrator = getattr(_BOOT_CONTEXT, "orchestrator", None) if _BOOT_CONTEXT else None
        if orchestrator is not None:
            loops = list(getattr(orchestrator, "loops", []) or [])
            for loop in loops:
                epic = str(getattr(loop, "_epic", "") or "")
                if not epic:
                    continue
                sampled = ring.read_quote_for_epic(epic)
                if sampled is None:
                    continue
                bid, offer, seq = sampled
                prev = last_quote_seq.get(epic)
                if prev is not None and prev == seq:
                    continue
                last_quote_seq[epic] = seq
                quote = Quote(datetime.now(timezone.utc), bid, offer)
                run_bare = getattr(loop, "run_bare_metal_unified_tick", None)
                if callable(run_bare):
                    try:
                        run_bare(quote)
                    except Exception as exc:
                        if not bare_metal_hot_path_active():
                            log_guarded_exception("unified_thread_b_bare_metal", exc)
        _STOP.wait(spin_sec)

    _STATE["thread_b_alive"] = False


def start_unified_engine(*, boot_context: Any | None = None) -> None:
    """Spawn Thread A + Thread B inside the master process."""
    global _THREAD_A, _THREAD_B, _BOOT_CONTEXT

    if _THREAD_A is not None and _THREAD_A.is_alive():
        return

    _BOOT_CONTEXT = boot_context
    _STOP.clear()

    from system.ipc.ring_buffer import get_alpha_ring_buffer

    get_alpha_ring_buffer()  # pre-allocate before threads

    _THREAD_A = threading.Thread(
        target=_shadow_coprocessor_loop,
        name="unified-thread-a-coprocessor",
        daemon=True,
    )
    _THREAD_B = threading.Thread(
        target=_live_execution_loop,
        name="unified-thread-b-live-exec",
        daemon=True,
    )
    _THREAD_A.start()
    _THREAD_B.start()

    try:
        from system.unified_fulfillment_cache import start_fulfillment_cache_refresh

        start_fulfillment_cache_refresh()
    except Exception as exc:
        log_guarded_exception("unified_fulfillment_cache", exc)

    _STATE.update(
        {
            "running": True,
            "thread_a_alive": True,
            "thread_b_alive": True,
            "started_at_epoch": time.time(),
        }
    )
    log_engine(
        "UnifiedEngine: master process threads armed "
        f"Thread-A={_THREAD_A.name} Thread-B={_THREAD_B.name} "
        f"ring_bytes={get_alpha_ring_buffer()._matrix.nbytes}"
    )


def stop_unified_engine() -> None:
    _STOP.set()
    _STATE["running"] = False
    try:
        from system.unified_fulfillment_cache import stop_fulfillment_cache_refresh

        stop_fulfillment_cache_refresh()
    except Exception:
        pass
    try:
        from system.feeds.multi_feed_hub import stop_racing_multi_feed_hub

        stop_racing_multi_feed_hub()
    except Exception:
        pass


def unified_performance_payload() -> dict[str, Any]:
    from datetime import datetime, timezone

    from system.ipc.ring_buffer import get_alpha_ring_buffer

    ring = get_alpha_ring_buffer()
    tel = ring.telemetry()
    feed: dict[str, Any] = {}
    try:
        from system.feeds.multi_feed_hub import feed_hub_telemetry

        feed = feed_hub_telemetry()
    except Exception:
        feed = {"stream_mapping_banner": "🔴 Feed hub unavailable"}

    return {
        "mode": "UNIFIED_ENGINE",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thread_alignment": {
            "synced": bool(tel.get("thread_aligned")),
            "thread_a_write_seq": tel.get("thread_a_write_seq"),
            "thread_b_read_seq": tel.get("thread_b_read_seq"),
            "compile_generation": tel.get("compile_generation"),
        },
        "e2e_latency_ns": tel.get("e2e_latency_ns") or {},
        "vector_density": int(tel.get("vector_density") or 0),
        "multi_feed_hub": feed,
        "stream_mapping_banner": feed.get(
            "stream_mapping_banner",
            "🟢 Yahoo + Finnhub + Twelve Data Mapped (Absolute Feed Resilience)",
        ),
        "threads": {
            "a_alive": bool(_THREAD_A and _THREAD_A.is_alive()),
            "b_alive": bool(_THREAD_B and _THREAD_B.is_alive()),
            "master_pid": os.getpid(),
            "multi_feed_hub": bool(_STATE.get("multi_feed_hub")),
        },
        "ring": tel,
    }


def reset_unified_engine_for_tests() -> None:
    stop_unified_engine()
    global _THREAD_A, _THREAD_B, _BOOT_CONTEXT
    _THREAD_A = None
    _THREAD_B = None
    _BOOT_CONTEXT = None
    _STATE.clear()
    _STATE.update({"running": False, "thread_a_alive": False, "thread_b_alive": False})
