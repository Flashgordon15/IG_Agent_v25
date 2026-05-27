"""
Runtime health checks for 24/7 operation and Ready To Go Live validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from system.engine_log import log_engine


class HealthStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Single checklist item result."""

    name: str
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """
    Aggregates component health for dashboard and go-live checklist.

    Checks include: credentials, REST login, streaming, balance, epic, spread,
    ATR, journal writable, learning DB writable, execution engine.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._checks: dict[str, Callable[[], HealthCheckResult]] = {}
        self._last_results: list[HealthCheckResult] = []

    def register(self, name: str, check_fn: Callable[[], HealthCheckResult]) -> None:
        """Register a named health check function."""
        self._checks[name] = check_fn

    def run_all(self) -> list[HealthCheckResult]:
        """Execute all registered checks and return results."""
        results: list[HealthCheckResult] = []
        for name, check_fn in self._checks.items():
            try:
                results.append(check_fn())
            except Exception as e:
                results.append(
                    HealthCheckResult(name, HealthStatus.FAIL, str(e), {})
                )
        self._last_results = results
        return results

    def run_go_live_checklist(
        self,
        *,
        rest_client: Any = None,
        stream_client: Any = None,
        execution_engine: Any = None,
        bot: Any = None,
        probe_streaming: bool = True,
    ) -> list[HealthCheckResult]:
        """
        Run full Ready To Go Live validator via startup pipeline.

        :returns: List of check results for UI green/red indicators.
        """
        from system.demo_readiness import ReadinessCheckResult
        from system.startup_pipeline import run_startup_pipeline

        report = run_startup_pipeline(
            probe_streaming=probe_streaming,
            bot=bot,
            force_refresh=True,
        )
        results = [_from_readiness(c) for c in report.checks]
        if rest_client is not None:
            results.append(
                HealthCheckResult(
                    "rest_client_attached",
                    HealthStatus.OK,
                    "REST client supplied",
                    {},
                )
            )
        if stream_client is not None:
            st = getattr(stream_client, "state", None)
            msg = f"stream state={st}" if st is not None else "stream client supplied"
            results.append(
                HealthCheckResult("stream_client_attached", HealthStatus.OK, msg, {})
            )
        if execution_engine is not None:
            results.append(
                HealthCheckResult(
                    "execution_engine_attached",
                    HealthStatus.OK,
                    "Execution engine supplied",
                    {},
                )
            )
        self._last_results = results
        log_engine(
            f"HealthMonitor go-live: {sum(1 for r in results if r.status == HealthStatus.OK)}"
            f"/{len(results)} OK"
        )
        return results

    def is_healthy(self) -> bool:
        """True if all checks are OK (no FAIL)."""
        results = self._last_results or self.run_go_live_checklist(probe_streaming=False)
        return all(r.status != HealthStatus.FAIL for r in results)


def _from_readiness(check: Any) -> HealthCheckResult:
    from system.demo_readiness import ReadinessCheckResult

    if isinstance(check, ReadinessCheckResult):
        status = HealthStatus.OK if check.ok else HealthStatus.FAIL
        return HealthCheckResult(check.name, status, check.message, check.details)
    ok = bool(getattr(check, "ok", False))
    return HealthCheckResult(
        str(getattr(check, "name", "unknown")),
        HealthStatus.OK if ok else HealthStatus.FAIL,
        str(getattr(check, "message", "")),
        dict(getattr(check, "details", {}) or {}),
    )
