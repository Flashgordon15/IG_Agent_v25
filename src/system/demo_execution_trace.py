"""
DEMO execution pipeline tracing — diagnostics for IG demo order routing.

Logs to src/data/logs/demo_execution_trace.log and exposes a UI snapshot.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from system.paths import logs_dir

_TRACE_LOG = logs_dir() / "demo_execution_trace.log"
_LOCK = threading.Lock()
_MAX_UI_LINES = 80


@dataclass
class DemoDiagnosticsSnapshot:
    operating_mode: str = ""
    credentials_account_type: str = ""
    executor_selected: str = ""
    last_stage: str = ""
    last_function: str = ""
    last_decision: str = ""
    last_next: str = ""
    last_signal: str = ""
    auto_trade_enabled: bool = False
    execute_trade_called: bool = False
    last_order_payload: dict[str, Any] = field(default_factory=dict)
    last_ig_response: dict[str, Any] = field(default_factory=dict)
    last_rejection: str = ""
    account_fetch_error: str = ""
    simulator_fallback: bool = False
    streaming_status: str = ""
    rest_status: str = ""
    account_id: str = ""
    endpoint: str = ""
    rest_login_endpoint: str = ""
    rest_login_payload_masked: str = ""
    rest_login_status_code: int | None = None
    cst_token: str = ""
    security_token: str = ""
    streaming_auth_status: str = ""
    fallback_reason: str = ""
    rate_limit_active: bool = False
    rate_limit_countdown: str = ""
    last_403_timestamp: str = ""
    blocked_calls_count: int = 0
    backoff_stage: int = 0
    ig_api_readiness: bool = False
    last_readiness_check_at: str = ""
    last_readiness_http_code: int | None = None
    last_readiness_error_code: str = ""
    readiness_next_step: str = ""
    ig_open_positions_total: int = 0
    ig_open_positions_by_epic: str = ""
    ig_account_upl: float = 0.0
    ig_position_sync_status: str = ""
    ig_position_sync_at: str = ""
    last_ig_event: str = ""
    last_closed_trade_summary: str = ""
    recent_trace: list[str] = field(default_factory=list)


_snapshot = DemoDiagnosticsSnapshot()


def get_demo_diagnostics_snapshot() -> DemoDiagnosticsSnapshot:
    with _LOCK:
        return DemoDiagnosticsSnapshot(
            operating_mode=_snapshot.operating_mode,
            credentials_account_type=_snapshot.credentials_account_type,
            executor_selected=_snapshot.executor_selected,
            last_stage=_snapshot.last_stage,
            last_function=_snapshot.last_function,
            last_decision=_snapshot.last_decision,
            last_next=_snapshot.last_next,
            last_signal=_snapshot.last_signal,
            auto_trade_enabled=_snapshot.auto_trade_enabled,
            execute_trade_called=_snapshot.execute_trade_called,
            last_order_payload=dict(_snapshot.last_order_payload),
            last_ig_response=dict(_snapshot.last_ig_response),
            last_rejection=_snapshot.last_rejection,
            account_fetch_error=_snapshot.account_fetch_error,
            simulator_fallback=_snapshot.simulator_fallback,
            streaming_status=_snapshot.streaming_status,
            rest_status=_snapshot.rest_status,
            account_id=_snapshot.account_id,
            endpoint=_snapshot.endpoint,
            rest_login_endpoint=_snapshot.rest_login_endpoint,
            rest_login_payload_masked=_snapshot.rest_login_payload_masked,
            rest_login_status_code=_snapshot.rest_login_status_code,
            cst_token=_snapshot.cst_token,
            security_token=_snapshot.security_token,
            streaming_auth_status=_snapshot.streaming_auth_status,
            fallback_reason=_snapshot.fallback_reason,
            rate_limit_active=_snapshot.rate_limit_active,
            rate_limit_countdown=_snapshot.rate_limit_countdown,
            last_403_timestamp=_snapshot.last_403_timestamp,
            blocked_calls_count=_snapshot.blocked_calls_count,
            backoff_stage=_snapshot.backoff_stage,
            ig_api_readiness=_snapshot.ig_api_readiness,
            last_readiness_check_at=_snapshot.last_readiness_check_at,
            last_readiness_http_code=_snapshot.last_readiness_http_code,
            last_readiness_error_code=_snapshot.last_readiness_error_code,
            readiness_next_step=_snapshot.readiness_next_step,
            ig_open_positions_total=_snapshot.ig_open_positions_total,
            ig_open_positions_by_epic=_snapshot.ig_open_positions_by_epic,
            ig_account_upl=_snapshot.ig_account_upl,
            ig_position_sync_status=_snapshot.ig_position_sync_status,
            ig_position_sync_at=_snapshot.ig_position_sync_at,
            last_ig_event=_snapshot.last_ig_event,
            last_closed_trade_summary=_snapshot.last_closed_trade_summary,
            recent_trace=list(_snapshot.recent_trace),
        )


def demo_trace_log_path() -> str:
    return str(_TRACE_LOG)


def _should_trace() -> bool:
    try:
        from system.config_loader import get_mode

        return get_mode() in ("DEMO", "LIVE")
    except Exception:
        return True


def _safe_json(obj: Any, limit: int = 1200) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def trace_execution(
    stage: str,
    func: str,
    *,
    decision: str = "",
    next_fn: str = "",
    params: dict[str, Any] | None = None,
) -> None:
    """Append a pipeline trace line and update the UI snapshot."""
    if not _should_trace():
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    param_str = _safe_json(params or {})
    line = (
        f"{ts} | {stage} | {func} | entered"
        f" | decision={decision or '-'}"
        f" | next={next_fn or '-'}"
        f" | params={param_str}"
    )

    _TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with open(_TRACE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _snapshot.last_stage = stage
        _snapshot.last_function = func
        if decision:
            _snapshot.last_decision = decision
        if next_fn:
            _snapshot.last_next = next_fn
        _snapshot.recent_trace.append(line)
        if len(_snapshot.recent_trace) > _MAX_UI_LINES:
            _snapshot.recent_trace = _snapshot.recent_trace[-_MAX_UI_LINES:]


def update_demo_diagnostics(**kwargs: Any) -> None:
    with _LOCK:
        for k, v in kwargs.items():
            if hasattr(_snapshot, k):
                setattr(_snapshot, k, v)


def log_mode_routing(
    *,
    operating_mode: str,
    credentials_account_type: str,
    executor_selected: str,
    endpoint: str = "",
    account_id: str = "",
) -> None:
    update_demo_diagnostics(
        operating_mode=operating_mode,
        credentials_account_type=credentials_account_type,
        executor_selected=executor_selected,
        endpoint=endpoint,
        account_id=account_id,
    )
    if operating_mode == "DEMO":
        trace_execution(
            "MODE",
            "mode_selector",
            decision="DEMO mode routing active — using live_executor (DEMO endpoints)",
            next_fn="ExecutionEngine.execute_trade",
            params={
                "operating_mode": operating_mode,
                "ig_account_type": credentials_account_type,
                "executor": executor_selected,
                "endpoint": endpoint,
                "account_id": account_id,
            },
        )
    elif operating_mode == "LIVE":
        trace_execution(
            "MODE",
            "mode_selector",
            decision="LIVE mode routing active — using live_executor (LIVE endpoints)",
            next_fn="ExecutionEngine.execute_trade",
            params={
                "operating_mode": operating_mode,
                "executor": executor_selected,
            },
        )
    elif operating_mode == "TEST":
        trace_execution(
            "MODE",
            "mode_selector",
            decision="TEST mode routing — using simulator_executor",
            next_fn="TestSimulator.execute",
            params={"operating_mode": operating_mode},
        )


def log_simulator_fallback_warning(reason: str) -> None:
    update_demo_diagnostics(simulator_fallback=True, last_rejection=reason)
    trace_execution(
        "WARNING",
        "mode_selector",
        decision="WARNING: DEMO order routing fell back to simulator — investigate",
        params={"reason": reason},
    )


def clear_demo_execution_trace() -> None:
    with _LOCK:
        global _snapshot
        _snapshot = DemoDiagnosticsSnapshot()
    _TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    _TRACE_LOG.write_text("", encoding="utf-8")
