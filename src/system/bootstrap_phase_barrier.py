"""
Atomic Bootstrap Phase Barrier — deterministic kernel sequencing.

Thread A (multi-feed) and Thread B (live execution) must not run until:
  1. 4-way API connectivity pass completes successfully
  2. Authenticated ``rest_client`` is hard-committed to every per-epic TradingLoop
"""

from __future__ import annotations

import threading
import time
from typing import Any

_BARRIER = threading.Event()
_BARRIER_ARMED = False
_COMMITTED_REST: Any | None = None
_BIND_COUNT = 0
_API_REPORT: dict[str, Any] | None = None
_LOCK = threading.Lock()


def bootstrap_barrier_armed() -> bool:
    with _LOCK:
        return bool(_BARRIER_ARMED)


def committed_rest_client() -> Any | None:
    with _LOCK:
        return _COMMITTED_REST


def wait_bootstrap_phase_barrier(*, role: str = "thread", timeout_sec: float = 120.0) -> bool:
    """Block unified-engine threads until the bootstrap kernel arms the barrier."""
    if _BARRIER.is_set():
        return True
    from system.engine_log import log_engine

    log_engine(f"BootstrapBarrier: {role} waiting for phase barrier")
    ok = _BARRIER.wait(timeout=max(0.1, float(timeout_sec)))
    if ok:
        log_engine(f"BootstrapBarrier: {role} released")
    else:
        log_engine(f"BootstrapBarrier: {role} TIMEOUT after {timeout_sec}s")
    return ok


def _api_pass_acceptable(report: dict[str, Any]) -> bool:
    if report.get("skipped"):
        return True
    if report.get("all_ok"):
        return True
    results = report.get("results") or {}
    if not results:
        return False
    if report.get("yahoo_bypass"):
        return all(
            r.get("ok")
            for name, r in results.items()
            if name != "Yahoo Finance"
        )
    return False


def _production_api_override_ok(
    rest: Any,
    api_report: dict[str, Any] | None,
    log_fn: Any,
) -> bool:
    """Trust authenticated IGRestClient when auxiliary feed probes lag."""
    try:
        from ig_api.rest_client import IGRestClient
        from system.agent_execution_mode import (
            authentic_demo_broker_required,
            production_execution_active,
        )

        if not production_execution_active() and not authentic_demo_broker_required():
            return False
        if not isinstance(rest, IGRestClient):
            return False
        if api_report:
            ig_row = (api_report.get("results") or {}).get("IG Trading Client") or {}
            if ig_row.get("ok"):
                log_fn(
                    "BootstrapBarrier: production override — IGRestClient validated, "
                    "arming despite auxiliary feed probe timeout"
                )
                return True
        session = getattr(rest, "session", None)
        if session and getattr(session, "is_valid", False):
            log_fn(
                "BootstrapBarrier: production override — authenticated IGRestClient session trusted"
            )
            return True
    except Exception:
        pass
    return False


def commit_rest_client_to_trading_loop(loop: Any, rest_client: Any) -> bool:
    """Hard-bind authenticated REST handle on a single TradingLoop instance."""
    if rest_client is None:
        return False
    try:
        exec_loop = loop._execution_loop  # noqa: SLF001
        engine = exec_loop.execution_engine
        if hasattr(engine, "commit_rest_client"):
            engine.commit_rest_client(rest_client)
        else:
            engine._rest_client = rest_client  # noqa: SLF001
            validator = getattr(engine, "_validator", None)
            if validator is not None and hasattr(validator, "attach_rest_client"):
                validator.attach_rest_client(rest_client)
            if getattr(engine, "mode", None) and engine.mode.uses_broker():
                from execution.live_executor import LiveExecutor

                engine._live = LiveExecutor(engine.config, rest_client)  # noqa: SLF001
        loop._broker_barrier_committed = True  # noqa: SLF001
        return True
    except Exception:
        return False


def commit_rest_client_to_all_loops(*, boot_context: Any) -> int:
    """Propagate BootContext.rest_client to every orchestrator TradingLoop."""
    global _COMMITTED_REST, _BIND_COUNT

    rest = getattr(boot_context, "rest_client", None)
    orch = getattr(boot_context, "orchestrator", None)
    if rest is None or orch is None:
        return 0

    loops = list(getattr(orch, "loops", []) or [])
    bound = 0
    for loop in loops:
        if commit_rest_client_to_trading_loop(loop, rest):
            bound += 1

    with _LOCK:
        _COMMITTED_REST = rest
        _BIND_COUNT = bound
    return bound


def resync_trading_loop_broker(loop: Any) -> bool:
    """Internal re-synchronization — pull committed REST handle onto a loop."""
    rest = committed_rest_client()
    if rest is None:
        try:
            from system.unified_engine import get_boot_context

            ctx = get_boot_context()
            rest = getattr(ctx, "rest_client", None) if ctx is not None else None
        except Exception:
            rest = None
    if rest is None:
        return False
    return commit_rest_client_to_trading_loop(loop, rest)


def execute_atomic_bootstrap_phase_barrier(
    boot_context: Any,
    *,
    verify_timeout_sec: float = 5.0,
    emit: bool = True,
) -> dict[str, Any]:
    """
    Run API verify → REST bind → arm barrier. Returns structured report.
    Must complete before ``start_unified_engine()`` spawns Thread A/B.
    """
    global _BARRIER_ARMED, _API_REPORT

    from system.engine_log import log_engine

    rest = getattr(boot_context, "rest_client", None)
    orch = getattr(boot_context, "orchestrator", None)

    try:
        from system.agent_execution_mode import (
            authentic_demo_broker_required,
            production_execution_active,
        )

        if production_execution_active() or authentic_demo_broker_required():
            from ig_api.mock_clients import MockIGRest
            from system.ig_rest_session import force_authenticated_ig_rest_client

            if rest is None or isinstance(rest, MockIGRest):
                rest = force_authenticated_ig_rest_client()
                boot_context.rest_client = rest
    except Exception as exc:
        report = {
            "phase": "PRODUCTION_REST_FORCE_FAILED",
            "api_ok": False,
            "rest_bound": 0,
            "loops_expected": len(getattr(orch, "loops", []) or []) if orch else 0,
            "armed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        log_engine(f"BootstrapBarrier: production IGRestClient force failed — {report['error']}")
        return report

    report: dict[str, Any] = {
        "phase": "PENDING",
        "api_ok": False,
        "rest_bound": 0,
        "loops_expected": len(getattr(orch, "loops", []) or []) if orch else 0,
        "armed": False,
    }

    if rest is None:
        report["phase"] = "REST_MISSING"
        report["error"] = "BootContext.rest_client is None"
        log_engine("BootstrapBarrier: FATAL — no authenticated rest_client on BootContext")
        return report

    if orch is None:
        report["phase"] = "ORCHESTRATOR_MISSING"
        report["error"] = "BootContext.orchestrator is None"
        log_engine("BootstrapBarrier: FATAL — orchestrator not built before barrier")
        return report

    report["phase"] = "API_VERIFY"
    prod_timeout = verify_timeout_sec
    try:
        from system.agent_execution_mode import production_execution_active

        if production_execution_active():
            prod_timeout = max(float(verify_timeout_sec), 10.0)
    except Exception:
        pass
    try:
        from system.feeds.multi_feed_hub import verify_all_api_pipelines

        if emit:
            print("\033[1m=== 4-WAY API PIPELINE CONNECTIVITY PASS ===\033[0m", flush=True)
        api_report = verify_all_api_pipelines(
            rest_client=rest,
            timeout_sec=prod_timeout,
            emit=emit,
        )
        if emit:
            print("\033[1m=== END API PIPELINE PASS ===\033[0m\n", flush=True)
        _API_REPORT = api_report
        report["api_report"] = api_report
        report["api_ok"] = _api_pass_acceptable(api_report)
        if not report["api_ok"]:
            report["api_ok"] = _production_api_override_ok(rest, api_report, log_engine)
    except Exception as exc:
        api_report = None
        try:
            from system.feeds.multi_feed_hub import api_pipeline_verify_report

            api_report = api_pipeline_verify_report()
        except Exception:
            api_report = None
        if api_report:
            _API_REPORT = api_report
            report["api_report"] = api_report
            report["api_ok"] = _api_pass_acceptable(api_report)
        if not report.get("api_ok"):
            report["api_ok"] = _production_api_override_ok(rest, api_report, log_engine)
        if not report.get("api_ok"):
            report["phase"] = "API_VERIFY_FAILED"
            report["error"] = f"{type(exc).__name__}: {exc}"
            log_engine(f"BootstrapBarrier: API verify failed — {report['error']}")
            return report
        log_engine(
            f"BootstrapBarrier: API verify exception recovered — {type(exc).__name__}: {exc}"
        )

    if not report["api_ok"]:
        report["phase"] = "API_VERIFY_INCOMPLETE"
        log_engine("BootstrapBarrier: API connectivity pass did not complete successfully")
        return report

    report["phase"] = "REST_BIND"
    bound = commit_rest_client_to_all_loops(boot_context=boot_context)
    report["rest_bound"] = bound
    expected = report["loops_expected"]
    if bound < expected or bound == 0:
        report["phase"] = "REST_BIND_INCOMPLETE"
        report["error"] = f"bound {bound}/{expected} loops"
        log_engine(f"BootstrapBarrier: REST bind incomplete — {report['error']}")
        return report

    report["phase"] = "ARMED"
    with _LOCK:
        _BARRIER_ARMED = True
    _BARRIER.set()
    report["armed"] = True
    log_engine(
        f"BootstrapBarrier: ARMED api_ok=True rest_bound={bound}/{expected} "
        f"rest_type={type(rest).__name__}"
    )
    return report


def barrier_status() -> dict[str, Any]:
    with _LOCK:
        return {
            "armed": _BARRIER_ARMED,
            "bind_count": _BIND_COUNT,
            "has_rest": _COMMITTED_REST is not None,
            "api_report": _API_REPORT,
        }
