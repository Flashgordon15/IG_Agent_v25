"""Non-blocking Gate 2 hydration — positions, orders, balance, size rules prefetch."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

from system.engine_log import log_engine


_HYDRATION_THREAD: threading.Thread | None = None
_HYDRATION_DONE = threading.Event()


def hydration_complete() -> bool:
    return _HYDRATION_DONE.is_set()


def start_gate2_background_hydration(
    rest: Any,
    context: Any,
    state: Any | None = None,
) -> None:
    """Start background G2 work; Gate 2 may complete before this finishes."""
    global _HYDRATION_THREAD

    def _worker() -> None:
        global _HYDRATION_THREAD
        started = time.perf_counter()
        try:
            _hydrate_positions_orders(rest, context)
            _prefetch_size_rules(rest)
            _mark_subsystem_ok(state, "ig", "hydrated")
            elapsed = (time.perf_counter() - started) * 1000.0
            log_engine(f"Gate2-async: hydration complete ({elapsed:.0f}ms)")
        except Exception as exc:
            log_engine(
                f"Gate2-async: hydration degraded {type(exc).__name__}: {exc}"
            )
            _mark_subsystem_ok(state, "ig", "degraded", str(exc))
        finally:
            _HYDRATION_DONE.set()
            _HYDRATION_THREAD = None

    if _HYDRATION_THREAD is not None and _HYDRATION_THREAD.is_alive():
        return
    _HYDRATION_DONE.clear()
    _HYDRATION_THREAD = threading.Thread(
        target=_worker,
        name="gate2-async-hydrate",
        daemon=True,
    )
    _HYDRATION_THREAD.start()
    log_engine("Gate2-async: background hydration started")


def _hydrate_positions_orders(rest: Any, context: Any) -> None:
    from system.boot.gate2_runner import _fetch_working_orders

    def _run() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int]:
        positions = rest.open_positions()
        open_count = len(
            [
                p
                for p in positions
                if float((p.get("position") or {}).get("size") or 0) > 0
            ]
        )
        orders = _fetch_working_orders(rest)
        balance = rest.refresh_account_summary()
        return positions, orders, balance, open_count

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="g2-bg-hydrate") as pool:
        future = pool.submit(_run)
        positions, orders, balance, open_count = future.result(timeout=12.0)

    context.hydration_detail = {
        "open_positions": open_count,
        "working_orders": len(orders),
        "balance": balance.get("balance"),
        "available": balance.get("available"),
        "profit_loss": balance.get("profit_loss"),
        "async": True,
    }
    from api.snapshot_store import set_boot_hydration

    set_boot_hydration(positions, orders)
    log_engine(
        f"Gate2-async: positions={open_count} orders={len(orders)}"
    )


def _prefetch_size_rules(rest: Any) -> None:
    try:
        from runtime.dual_core_execution import ROTATION_UNIVERSE
        from execution.broker_epic_resolver import resolve_account_product, resolve_order_epic
        from execution.ig_size_validator import validate_order_size
        from system.config_loader import get_config

        cfg = get_config()
        product = resolve_account_product(rest=rest, cfg=cfg)
        loaded = 0
        for epic in list(ROTATION_UNIVERSE)[:7]:
            bepic = resolve_order_epic(epic, account_product=product)
            try:
                validate_order_size(
                    epic,
                    1.0,
                    "BUY",
                    cfg,
                    rest,
                    broker_epic=bepic,
                )
                loaded += 1
            except Exception:
                pass
        try:
            from system.unified_runtime_state import update_sizing, emit_event

            update_sizing(rules_loaded=loaded > 0)
            emit_event(
                "size_rules_prefetch",
                {"epics_loaded": loaded, "total": len(ROTATION_UNIVERSE)},
            )
        except Exception:
            pass
        log_engine(f"Gate2-async: size rules prefetched for {loaded} epics")
    except Exception as exc:
        log_engine(f"Gate2-async: size prefetch skipped: {type(exc).__name__}: {exc}")


def _mark_subsystem_ok(
    state: Any | None,
    subsystem: str,
    status: str,
    detail: str = "",
) -> None:
    try:
        from system.boot.boot_orchestrator import SubsystemId, StepStatus, mark_subsystem

        sid = SubsystemId.IG if subsystem == "ig" else None
        if sid is not None:
            st = StepStatus.OK if status == "hydrated" else StepStatus.DEGRADED
            mark_subsystem(sid, st, error=detail if st == StepStatus.DEGRADED else "")
    except Exception:
        pass
    try:
        from system.unified_runtime_state import update_feeds

        update_feeds(ig_ok=(status == "hydrated"))
    except Exception:
        pass
