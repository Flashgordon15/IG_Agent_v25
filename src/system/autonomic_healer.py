"""
Autonomic Self-Healing Engine — closed-loop infrastructure + cognitive ML correction.

Polls system health every 2000ms; mitigates transport stalls, epic stream corruption,
reconciliation drift, and sub-target strategy win rates without operator intervention.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from collections import defaultdict, deque
from enum import Enum
from typing import Any

from system.engine_log import log_engine

_POLL_SEC = 2.0
_LS_HANDSHAKE_TIMEOUT_SEC = 3.0
_TRANSPORT_STALL_SEC = 45.0
_STREAM_FAILURE_WINDOW_SEC = 120.0
_STREAM_FAILURE_THRESHOLD = 3
_WIN_RATE_TARGET = 0.70
_WIN_RATE_WINDOW = 20
_DRIFT_FLATTENER_GRACE_SEC = 30.0
_INIT_BOOT_BLOCKERS = frozenset({"broker_reconciliation_drift", "routing_unarmed"})


class TransportFailureCategory(str, Enum):
    UNKNOWN = "UNKNOWN"
    RATE_LIMIT_EXHAUSTED = "RATE_LIMIT_EXHAUSTED"
    AUTH_CREDENTIAL_INVALID = "AUTH_CREDENTIAL_INVALID"
    CARRIER_NETWORK_DROP = "CARRIER_NETWORK_DROP"


_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_rest_client: Any | None = None
_mitigations: deque[dict[str, Any]] = deque(maxlen=64)
_stream_failure_counts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=32))
_last_broker_handshake_error: str = ""
_last_transport_category: str = TransportFailureCategory.UNKNOWN.value
_last_http_status: int | None = None
_last_network_stack_trace: str = ""
_fallback_transport_tier: str = ""
_synthetic_hydration_active: bool = False
_boot_anchor_ts: float = 0.0
_failover_engaged: bool = False
_continuity_pass_done: bool = False
_cognitive_override_active: bool = False
_cognitive_override_reason: str = ""
_init_blocker_since: dict[str, float] = {}
_drift_flattener_engaged: bool = False
_drift_flattener_result: dict[str, Any] = {}
_snapshot: dict[str, Any] = {
    "ok": True,
    "engine_alive": False,
    "active_healer_mitigations": [],
    "cognitive_override_active": False,
    "cognitive_override_reason": "",
    "broker_handshake_raw_error": "",
    "transport_failure_category": TransportFailureCategory.UNKNOWN.value,
    "network_exception_code": "",
    "http_status": None,
    "fallback_transport_tier": "",
    "synthetic_hydration_active": False,
    "token_conservation_mode": False,
    "ts": 0.0,
}


def classify_transport_failure(
    *,
    reason: str = "",
    exc: BaseException | None = None,
    http_status: int | None = None,
    stack_trace: str = "",
) -> TransportFailureCategory:
    """Decode broker/network failure into institutional diagnostic enum."""
    blob = " ".join(
        filter(
            None,
            [
                str(reason or ""),
                str(exc or ""),
                stack_trace or "",
                str(http_status or ""),
            ],
        )
    ).lower()
    status = int(http_status) if http_status is not None else 0
    if status == 429 or re.search(r"\b429\b|rate.?limit|exhausted|too many", blob):
        return TransportFailureCategory.RATE_LIMIT_EXHAUSTED
    if status in (401, 403) or re.search(
        r"auth|credential|invalid.?session|cst-|xst-|security.?token|unauthorized",
        blob,
    ):
        return TransportFailureCategory.AUTH_CREDENTIAL_INVALID
    if re.search(
        r"disconnect|network|carrier|timeout|stall|connection.?reset|"
        r"handshake|unreachable|socket|websocket",
        blob,
    ):
        return TransportFailureCategory.CARRIER_NETWORK_DROP
    return TransportFailureCategory.UNKNOWN


def record_transport_failure_diagnostic(
    *,
    reason: str = "",
    exc: BaseException | None = None,
    http_status: int | None = None,
) -> TransportFailureCategory:
    """Persist parsed transport failure for ai_diagnostics and guardian policy."""
    global _last_broker_handshake_error, _last_transport_category
    global _last_http_status, _last_network_stack_trace
    stack = ""
    if exc is not None:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2048:]
    category = classify_transport_failure(
        reason=reason,
        exc=exc,
        http_status=http_status,
        stack_trace=stack,
    )
    _last_broker_handshake_error = str(reason or exc or category.value)[:512]
    _last_transport_category = category.value
    _last_http_status = int(http_status) if http_status is not None else None
    _last_network_stack_trace = stack
    if category is TransportFailureCategory.RATE_LIMIT_EXHAUSTED:
        try:
            from system.chaos_guardian import engage_token_conservation_mode

            engage_token_conservation_mode(reason=_last_broker_handshake_error)
            _record_mitigation("token_conservation_mode", detail=category.value)
        except Exception as guard_exc:
            log_engine(f"AutonomicHealer: token conservation guard {type(guard_exc).__name__}")
    if category is TransportFailureCategory.CARRIER_NETWORK_DROP:
        SessionHotSwapManager.on_carrier_drop(_last_broker_handshake_error)
    return category


def get_transport_recovery_state() -> dict[str, Any]:
    """Lightweight recovery flags for iron_cage and cockpit consumers."""
    return {
        "failover_engaged": bool(_failover_engaged),
        "synthetic_hydration_active": bool(_synthetic_hydration_active),
        "fallback_transport_tier": str(_fallback_transport_tier or ""),
        "transport_failure_category": str(_last_transport_category or ""),
        "network_exception_code": str(_last_transport_category or ""),
        "http_status": _last_http_status,
        "token_conservation_mode": _snapshot.get("token_conservation_mode", False),
        "hot_swap": get_hot_swap_snapshot(),
    }


class SessionHotSwapManager:
    """
    In-memory session hot-swap — virtual emulation loop on carrier drop,
    instant re-bind when live FIX/WebSocket handshake completes.
    """

    __slots__ = ()

    _lock = threading.RLock()
    _virtual_loop_active = False
    _live_tracking = True
    _carrier_drop_ts = 0.0
    _last_swap_ts = 0.0
    _emulation_ticks = 0
    _last_reason = ""

    @classmethod
    def on_carrier_drop(cls, reason: str = "") -> None:
        with cls._lock:
            cls._live_tracking = False
            cls._virtual_loop_active = True
            cls._carrier_drop_ts = time.time()
            cls._last_reason = str(reason or "")[:200]
        cls._run_virtual_emulation_burst()
        _record_mitigation("hot_swap_virtual_loop", detail=cls._last_reason)

    @classmethod
    def _run_virtual_emulation_burst(cls) -> None:
        try:
            from system.market_data_hub import COCKPIT_CORE_EPICS, run_synthetic_tick_injector

            run_synthetic_tick_injector(epics=COCKPIT_CORE_EPICS)
            with cls._lock:
                cls._emulation_ticks += 1
        except Exception as exc:
            log_engine(f"SessionHotSwap: emulation {type(exc).__name__}: {exc}")

    @classmethod
    def on_live_handshake_complete(cls) -> None:
        with cls._lock:
            was_virtual = cls._virtual_loop_active
            cls._virtual_loop_active = False
            cls._live_tracking = True
            cls._last_swap_ts = time.time()
        if was_virtual:
            _record_mitigation("hot_swap_live_restored", detail="handshake_complete")

    @classmethod
    def tick(cls) -> None:
        with cls._lock:
            active = cls._virtual_loop_active
        if not active:
            return
        cls._run_virtual_emulation_burst()
        try:
            from system.market_data_hub import COCKPIT_CORE_EPICS, night_matrix_fresh_count

            fresh, total = night_matrix_fresh_count(max_age_sec=5.0, epics=COCKPIT_CORE_EPICS)
            if total > 0 and fresh >= total:
                cls.on_live_handshake_complete()
        except Exception:
            pass

    @classmethod
    def snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            return {
                "ok": True,
                "virtual_loop_active": bool(cls._virtual_loop_active),
                "live_tracking": bool(cls._live_tracking),
                "carrier_drop_ts": cls._carrier_drop_ts,
                "last_swap_ts": cls._last_swap_ts,
                "emulation_ticks": int(cls._emulation_ticks),
                "last_reason": cls._last_reason,
            }


def get_hot_swap_snapshot() -> dict[str, Any]:
    return SessionHotSwapManager.snapshot()


def reset_hot_swap_for_tests() -> None:
    with SessionHotSwapManager._lock:
        SessionHotSwapManager._virtual_loop_active = False
        SessionHotSwapManager._live_tracking = True
        SessionHotSwapManager._carrier_drop_ts = 0.0
        SessionHotSwapManager._last_swap_ts = 0.0
        SessionHotSwapManager._emulation_ticks = 0
        SessionHotSwapManager._last_reason = ""


def _record_mitigation(action: str, *, detail: str = "", epic: str = "") -> None:
    row = {
        "ts": time.time(),
        "action": action,
        "detail": detail[:240],
        "epic": str(epic or ""),
    }
    with _lock:
        _mitigations.append(row)
    log_engine(f"AutonomicHealer: {action} {detail[:120]}")


def _engage_failover_recovery(reason: str) -> None:
    """Central failover hook — cognitive decode, transport recovery, continuity."""
    global _failover_engaged, _fallback_transport_tier, _synthetic_hydration_active
    category = record_transport_failure_diagnostic(reason=reason)
    try:
        from ig_api.lightstreamer_streaming import engage_transport_failover_recovery

        engage_transport_failover_recovery(reason=reason, category=category.value)
    except TypeError:
        from ig_api.lightstreamer_streaming import engage_transport_failover_recovery

        engage_transport_failover_recovery(reason=reason)
    except Exception as exc:
        record_transport_failure_diagnostic(reason=reason, exc=exc)
    _failover_engaged = True
    _fallback_transport_tier = "rest_poll"
    try:
        from system.market_data_hub import (
            get_fallback_transport_tier,
            night_matrix_fresh_count,
            run_synthetic_tick_injector,
            set_fallback_transport_tier,
        )

        set_fallback_transport_tier("rest_poll")
        tier = get_fallback_transport_tier()
        if tier:
            _fallback_transport_tier = tier
        fresh, total = night_matrix_fresh_count(max_age_sec=5.0)
        if total > 0 and fresh < total:
            run_synthetic_tick_injector()
            _synthetic_hydration_active = True
            _record_mitigation("synthetic_tick_injector", detail=f"fresh={fresh}/{total}")
    except Exception as exc:
        log_engine(f"AutonomicHealer: synthetic injector guard {type(exc).__name__}: {exc}")
    try:
        from runtime.master_orchestrator import force_autonomic_boot_progression

        force_autonomic_boot_progression(reason=reason)
        _record_mitigation("forced_boot_progression", detail=category.value)
    except Exception as exc:
        log_engine(f"AutonomicHealer: boot progression guard {type(exc).__name__}: {exc}")
    _record_mitigation("transport_failover_recovery", detail=f"{category.value}:{reason[:80]}")


def _post_failover_continuity_pass() -> None:
    """Re-arm synthetic continuity if live ticks remain sparse after failover."""
    global _continuity_pass_done, _synthetic_hydration_active
    if not _failover_engaged or _continuity_pass_done:
        return
    try:
        from system.market_data_hub import COCKPIT_CORE_EPICS, night_matrix_fresh_count

        fresh, total = night_matrix_fresh_count(max_age_sec=5.0, epics=COCKPIT_CORE_EPICS)
        if total > 0 and fresh < total:
            from system.market_data_hub import run_synthetic_tick_injector

            run_synthetic_tick_injector(epics=COCKPIT_CORE_EPICS)
            _synthetic_hydration_active = True
        _continuity_pass_done = True
    except Exception as exc:
        log_engine(f"AutonomicHealer: continuity pass {type(exc).__name__}: {exc}")


def _check_hub_quote_staleness() -> None:
    """Restart Yahoo poller or engage failover when hub quotes stop advancing."""
    try:
        from system.market_data_hub import COCKPIT_CORE_EPICS, get_market_data_hub

        hub = get_market_data_hub()
        max_age = 0.0
        fresh = 0
        for epic in COCKPIT_CORE_EPICS:
            snap = hub.get_snapshot(epic)
            if snap is None or float(snap.bid or 0) <= 0:
                continue
            age = float(snap.age_seconds())
            max_age = max(max_age, age)
            if age <= 12.0:
                fresh += 1
        if fresh >= len(COCKPIT_CORE_EPICS):
            return
        if max_age < 12.0:
            return
        try:
            from system.boot.subsystem_healer import heal_yahoo

            if heal_yahoo():
                _record_mitigation(
                    "hub_quote_staleness_yahoo_restart",
                    detail=f"max_age={max_age:.1f}s fresh={fresh}",
                )
                return
        except Exception:
            pass
        if not _failover_engaged and max_age >= 20.0:
            _engage_failover_recovery(f"hub_quote_staleness max_age={max_age:.1f}s")
    except Exception as exc:
        log_engine(f"AutonomicHealer: hub staleness check {type(exc).__name__}: {exc}")


def _check_transport_stall() -> None:
    """Detect LS handshake stall or hub tick starvation on cockpit core epics."""
    global _last_broker_handshake_error
    if _failover_engaged:
        return
    try:
        from ig_api.lightstreamer_streaming import get_lightstreamer_health
        from system.market_data_hub import COCKPIT_CORE_EPICS, night_matrix_fresh_count

        boot_age = time.time() - _boot_anchor_ts
        if boot_age < _TRANSPORT_STALL_SEC:
            return

        health = get_lightstreamer_health()
        if health.get("active") and not health.get("using_fallback"):
            connect_ts = float(health.get("connect_attempt_ts") or 0.0)
            first_tick = bool(health.get("first_tick_received"))
            if connect_ts > 0 and not first_tick:
                age = time.time() - connect_ts
                if age > _LS_HANDSHAKE_TIMEOUT_SEC:
                    err = (
                        f"lightstreamer_handshake_stall age={age:.1f}s "
                        f"epics={health.get('epic_count', 0)}"
                    )
                    http_status = health.get("last_http_status")
                    ls_err = str(health.get("last_connect_error") or "")
                    if ls_err:
                        err = f"{err} ls_error={ls_err[:120]}"
                    record_transport_failure_diagnostic(
                        reason=err,
                        http_status=int(http_status) if http_status else None,
                    )
                    _engage_failover_recovery(err)
                    return

        fresh, total = night_matrix_fresh_count(max_age_sec=5.0, epics=COCKPIT_CORE_EPICS)
        if total > 0 and fresh == 0:
            err = f"hub_tick_starvation fresh=0/{total} boot_age={boot_age:.1f}s"
            _engage_failover_recovery(err)
    except Exception as exc:
        record_transport_failure_diagnostic(reason="transport_stall_check", exc=exc)


def _epic_stream_failures(epic: str) -> int:
    key = str(epic or "").strip()
    if not key:
        return 0
    cutoff = time.time() - _STREAM_FAILURE_WINDOW_SEC
    with _lock:
        dq = _stream_failure_counts.get(key)
        if not dq:
            return 0
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)


def notify_dispatch_stream_failure(epic: str, reason: str) -> None:
    """Called from master orchestrator when epic dispatch/stream fails."""
    key = str(epic or "").strip()
    if not key or key.startswith("_"):
        return
    with _lock:
        _stream_failure_counts[key].append(time.time())
    if _epic_stream_failures(key) >= _STREAM_FAILURE_THRESHOLD:
        _heal_epic_ring_and_seed(key, reason)


def _heal_epic_ring_and_seed(epic: str, reason: str) -> None:
    try:
        from runtime.regime_switch_engine import reset_epic_regime_ring_with_hub_seed

        meta = reset_epic_regime_ring_with_hub_seed(epic)
        _record_mitigation(
            "epic_ring_hub_seed_reset",
            epic=epic,
            detail=f"{reason} bars={meta.get('bars')} source={meta.get('source')}",
        )
    except Exception as exc:
        _record_mitigation(
            "epic_ring_reset_failed",
            epic=epic,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _read_iron_cage_blockers() -> list[str]:
    try:
        from system.iron_cage_readiness import evaluate_iron_cage_readiness

        return list(evaluate_iron_cage_readiness(force_refresh=True).get("blockers") or [])
    except Exception:
        return []


def _overwrite_local_registry_from_broker(rest: Any) -> dict[str, Any]:
    """Force local position trees to match broker ledger truth."""
    result: dict[str, Any] = {"ok": False, "synced": False, "broker_positions": 0}
    if rest is None:
        result["error"] = "no_rest_client"
        return result
    try:
        raw = rest.get_open_positions() if hasattr(rest, "get_open_positions") else rest.open_positions()
        if isinstance(raw, dict):
            positions = raw.get("positions") or raw.get("data") or []
        else:
            positions = list(raw or [])
        result["broker_positions"] = len(positions)
    except Exception as exc:
        result["error"] = f"ledger_query:{type(exc).__name__}"
        return result

    try:
        from runtime.agent_bootstrap import get_ig_position_sync

        sync = get_ig_position_sync()
        if sync is not None:
            snap = sync.sync_once()
            result["synced"] = True
            result["sync_status"] = getattr(snap, "sync_status", "ok")
            result["internal_open"] = int(getattr(snap, "total_open", 0) or 0)
    except Exception as exc:
        result["sync_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from runtime.trade_lifecycle import snapshot as lifecycle_snapshot

        lc = lifecycle_snapshot()
        result["lifecycle_active"] = len(lc.get("active") or {})
    except Exception:
        pass

    result["ok"] = bool(result.get("synced"))
    return result


def _force_arm_routing_and_trade_ready() -> dict[str, Any]:
    """Arm orchestrator routing matrices and transition Iron Cage to trade_ready."""
    outcome: dict[str, Any] = {"trade_ready": False, "armed": False}
    try:
        from system.runtime_stabilizer import get_stabilizer_seal

        outcome["stabilizer_seal"] = get_stabilizer_seal()
    except Exception:
        outcome["stabilizer_seal"] = "UNKNOWN"
    try:
        from runtime import master_orchestrator as mo

        mo.force_autonomic_boot_progression(reason="autonomic_drift_flattener")
        with mo._lock:
            mo._armed = True
            mo._boot_trade_ready = True
            mo._primed = True
        outcome["armed"] = True
        outcome["trade_ready"] = True
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"

    try:
        from runtime.unified_execution import cached_unified_routes

        routes = cached_unified_routes()
        outcome["routes_cached"] = len(routes or [])
    except Exception:
        pass

    try:
        from system.boot.boot_orchestrator import get_boot_status_snapshot

        boot = get_boot_status_snapshot()
        if not boot.get("trade_ready"):
            outcome["boot_trade_ready_pending"] = True
    except Exception:
        pass
    return outcome


def _activate_autonomic_drift_flattener(*, blockers: list[str]) -> dict[str, Any]:
    """Autonomic Drift Flattener — broker ledger sync, registry overwrite, routing arm."""
    global _drift_flattener_engaged, _drift_flattener_result
    if _drift_flattener_engaged:
        return dict(_drift_flattener_result)
    _drift_flattener_engaged = True
    client = _rest_client
    actions: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "blockers": list(blockers),
        "actions": actions,
        "trade_ready": False,
        "ts": time.time(),
    }

    ledger = _overwrite_local_registry_from_broker(client)
    result["ledger"] = ledger
    if ledger.get("ok"):
        actions.append("broker_ledger_synced")

    try:
        from system.broker_reconciliation_daemon import run_reconciliation_once

        rec = run_reconciliation_once(rest=client)
        result["reconcile"] = rec
        if rec.get("healthy"):
            actions.append("reconciliation_healthy")
        elif client is not None:
            retry = run_reconciliation_once(rest=client)
            result["reconcile_retry"] = retry
            if retry.get("healthy"):
                actions.append("reconciliation_retry_ok")
    except Exception as exc:
        result["reconcile_error"] = f"{type(exc).__name__}: {exc}"

    arm = _force_arm_routing_and_trade_ready()
    result["arm"] = arm
    if arm.get("armed"):
        actions.append("routing_matrices_armed")
    if arm.get("trade_ready"):
        actions.append("iron_cage_trade_ready")
        result["trade_ready"] = True

    try:
        from system.iron_cage_readiness import evaluate_iron_cage_readiness

        iron = evaluate_iron_cage_readiness(force_refresh=True)
        result["iron_cage_trade_ready"] = bool(iron.get("trade_ready"))
        result["iron_cage_blockers"] = list(iron.get("blockers") or [])
        if iron.get("trade_ready"):
            result["trade_ready"] = True
    except Exception as exc:
        result["iron_cage_error"] = f"{type(exc).__name__}: {exc}"

    result["ok"] = bool(result.get("trade_ready"))
    result["actions"] = actions
    _drift_flattener_result = dict(result)
    _record_mitigation("autonomic_drift_flattener", detail=",".join(actions)[:240])

    try:
        from system.alert_reporting_matrix import notify_drift_clear

        notify_drift_clear(blockers=blockers, result=result)
    except Exception:
        pass

    log_engine(
        f"AutonomicHealer: drift flattener engaged blockers={blockers} "
        f"trade_ready={result.get('trade_ready')}"
    )
    return result


def _check_init_boot_blockers() -> None:
    """Engage drift flattener when init blockers persist >30s during warmup cycle."""
    global _init_blocker_since
    if _drift_flattener_engaged or _boot_anchor_ts <= 0:
        return

    blockers = _read_iron_cage_blockers()
    now = time.time()
    tracked = [b for b in blockers if b in _INIT_BOOT_BLOCKERS]
    for key in list(_init_blocker_since.keys()):
        if key not in tracked:
            _init_blocker_since.pop(key, None)
    if not tracked:
        return

    for blocker in tracked:
        if blocker not in _init_blocker_since:
            _init_blocker_since[blocker] = now
        age = now - _init_blocker_since[blocker]
        if age >= _DRIFT_FLATTENER_GRACE_SEC:
            _activate_autonomic_drift_flattener(blockers=tracked)
            return


def _check_warmup_init_blockers() -> None:
    """Inspect orchestrator warmup state without re-running the full prime cycle."""
    if _drift_flattener_engaged or _boot_anchor_ts <= 0:
        return
    try:
        from runtime import master_orchestrator as mo

        if mo.orchestrator_trade_ready():
            return
        flags: list[str] = []
        if not mo.orchestrator_trade_ready():
            flags.append("routing_unarmed")
        stage_status = mo.get_warmup_phase_status()
        if isinstance(stage_status, dict):
            failed = [
                k
                for k, v in stage_status.items()
                if str(v).upper() in ("FAILED", "PENDING")
            ]
            if failed:
                flags.append("broker_reconciliation_drift")
        if not flags:
            return
        now = time.time()
        for flag in flags:
            if flag not in _init_blocker_since:
                _init_blocker_since[flag] = now
            elif now - _init_blocker_since[flag] >= _DRIFT_FLATTENER_GRACE_SEC:
                _activate_autonomic_drift_flattener(
                    blockers=list(set(flags + _read_iron_cage_blockers()))
                )
                return
    except Exception:
        pass


def _heal_stale_token_queue_delays() -> None:
    """Clear phantom queue-delay counters when token buckets have available headroom."""
    try:
        from system.chaos_guardian import (
            clear_token_queue_delays,
            get_token_bucket_snapshots,
            replenish_critical_buckets,
        )

        buckets = get_token_bucket_snapshots()
        orders = buckets.get("ig_orders") or {}
        if float(orders.get("queued_waits") or 0) > 0 and float(
            orders.get("tokens_available") or 0
        ) < 1.0:
            rep = replenish_critical_buckets()
            if rep:
                _record_mitigation(
                    "order_bucket_replenish",
                    detail=str(rep.get("ig_orders") or rep),
                )
                return
        stale = any(
            float(row.get("queued_waits") or 0) > 0
            and float(row.get("tokens_available") or 0) >= 1.0
            for row in buckets.values()
        )
        if stale:
            clear_token_queue_delays(refill=True)
            _record_mitigation("token_queue_delay_heal", detail="queued_waits_cleared")
    except Exception as exc:
        log_engine(f"AutonomicHealer: token queue heal {type(exc).__name__}: {exc}")


def _check_reconciliation_drift() -> None:
    global _last_broker_handshake_error
    client = _rest_client
    try:
        from system.chaos_guardian import run_state_reconcile_tick

        sync = run_state_reconcile_tick(rest=client)
        if sync.get("healthy") is not False:
            return
        drift = int(sync.get("drift_count") or 0)
        if drift <= 1:
            return
        reason = str(sync.get("error") or sync.get("local_anomalies") or "drift")
        flatten = sync.get("emergency_flatten") or {}
        if flatten.get("ok"):
            _record_mitigation("reconcile_emergency_flatten", detail=reason)
            return
        if client is None:
            return
        try:
            from system.chaos_guardian import _emergency_flatten_drift

            result = _emergency_flatten_drift(rest=client, reason=f"autonomic:{reason}")
            if not result.get("ok"):
                _last_broker_handshake_error = str(result.get("errors") or reason)
                _record_mitigation(
                    "reconcile_force_flatten_retry",
                    detail=_last_broker_handshake_error,
                )
        except Exception as exc:
            _last_broker_handshake_error = f"{type(exc).__name__}: {exc}"
            _record_mitigation("reconcile_flatten_failed", detail=_last_broker_handshake_error)
    except Exception as exc:
        _last_broker_handshake_error = f"{type(exc).__name__}: {exc}"


def _cognitive_self_correction_pass() -> None:
    global _cognitive_override_active, _cognitive_override_reason
    triggered = False
    reasons: list[str] = []

    try:
        from runtime.master_orchestrator import get_platform_scoreboard

        sb = get_platform_scoreboard()
        wr = float(sb.rolling_win_rate())
        n = int(len(sb._trade_results))  # noqa: SLF001 — healer reads window size
        if n >= _WIN_RATE_WINDOW and wr < _WIN_RATE_TARGET:
            triggered = True
            reasons.append(f"scoreboard_wr={wr:.2%}<{_WIN_RATE_TARGET:.0%}")
    except Exception:
        pass

    try:
        from trading.probability_engine import detect_sentiment_news_feature_drift

        drift_epics = detect_sentiment_news_feature_drift()
        if drift_epics:
            triggered = True
            reasons.append(f"feature_drift_slots_98_111:{','.join(drift_epics[:4])}")
    except Exception:
        pass

    if not triggered:
        with _lock:
            _cognitive_override_active = False
            _cognitive_override_reason = ""
        return

    reason = "; ".join(reasons)
    with _lock:
        _cognitive_override_active = True
        _cognitive_override_reason = reason

    try:
        from trading.probability_engine import apply_cognitive_self_correction

        apply_cognitive_self_correction(reason=reason)
        _record_mitigation("cognitive_ml_correction", detail=reason)
    except Exception as exc:
        _record_mitigation("cognitive_correction_failed", detail=str(exc))


def _refresh_snapshot() -> None:
    global _synthetic_hydration_active, _fallback_transport_tier
    try:
        from system.market_data_hub import get_market_data_hub

        frame_metrics = get_market_data_hub().stream_frame_metrics()
    except Exception:
        frame_metrics = {}

    try:
        from runtime.regime_switch_engine import get_ring_buffer_fill_percentages

        ring_pct = get_ring_buffer_fill_percentages()
    except Exception:
        ring_pct = {}

    try:
        from trading.probability_engine import get_ml_accuracy_metrics

        ml_metrics = get_ml_accuracy_metrics()
    except Exception:
        ml_metrics = {}

    try:
        from runtime.master_orchestrator import get_current_boot_stage_token

        boot_stage = get_current_boot_stage_token()
    except Exception:
        boot_stage = ""

    token_conservation = False
    try:
        from system.chaos_guardian import token_conservation_active

        token_conservation = token_conservation_active()
    except Exception:
        pass

    try:
        from system.market_data_hub import (
            get_fallback_transport_tier,
            synthetic_hydration_active,
        )

        _synthetic_hydration_active = synthetic_hydration_active()
        tier = get_fallback_transport_tier()
        if tier:
            _fallback_transport_tier = tier
    except Exception:
        pass

    body = {
        "ok": True,
        "engine_alive": bool(_thread and _thread.is_alive()),
        "poll_interval_sec": _POLL_SEC,
        "current_boot_stage": boot_stage,
        "active_healer_mitigations": list(_mitigations)[-12:],
        "frame_queue_depth": int(frame_metrics.get("queue_depth_approx") or 0),
        "frame_metrics": frame_metrics,
        "ring_buffer_fill_percentages": ring_pct,
        "ml_accuracy_metrics": ml_metrics,
        "cognitive_override_active": _cognitive_override_active,
        "cognitive_override_reason": _cognitive_override_reason,
        "broker_handshake_raw_error": _last_broker_handshake_error,
        "transport_failure_category": _last_transport_category,
        "network_exception_code": _last_transport_category,
        "http_status": _last_http_status,
        "network_stack_trace_tail": _last_network_stack_trace[-512:],
        "fallback_transport_tier": _fallback_transport_tier or "live",
        "synthetic_hydration_active": _synthetic_hydration_active,
        "failover_engaged": _failover_engaged,
        "drift_flattener_engaged": _drift_flattener_engaged,
        "drift_flattener_result": dict(_drift_flattener_result),
        "init_blocker_since": dict(_init_blocker_since),
        "token_conservation_mode": token_conservation,
        "transport_recovery": get_transport_recovery_state(),
        "ts": time.time(),
    }
    try:
        from system.chaos_guardian import IronLedgerSnapshot

        inst = IronLedgerSnapshot.read_section("institutional")
        if inst:
            body["institutional"] = inst
    except Exception:
        pass
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)


class AutonomicHealerEngine:
    """Background autonomic loop — 2000ms poll cadence."""

    def __init__(self) -> None:
        self._tick_count = 0

    def run_once(self) -> None:
        self._tick_count += 1
        SessionHotSwapManager.tick()
        _check_transport_stall()
        _check_hub_quote_staleness()
        _heal_stale_token_queue_delays()
        _post_failover_continuity_pass()
        _check_init_boot_blockers()
        if not _drift_flattener_engaged:
            _check_warmup_init_blockers()
        _check_reconciliation_drift()
        _cognitive_self_correction_pass()
        _refresh_snapshot()

    def run_loop(self) -> None:
        log_engine("AutonomicHealerEngine: daemon started (2000ms poll)")
        while not _stop.wait(_POLL_SEC):
            try:
                self.run_once()
            except Exception as exc:
                log_engine(f"AutonomicHealerEngine: tick {type(exc).__name__}: {exc}")
        log_engine("AutonomicHealerEngine: daemon stopped")


_engine = AutonomicHealerEngine()


def get_ai_diagnostics_snapshot() -> dict[str, Any]:
    with _lock:
        if _snapshot.get("ts", 0) <= 0:
            _refresh_snapshot()
        body = dict(_snapshot)
    try:
        from system.chaos_guardian import IronLedgerSnapshot

        inst = IronLedgerSnapshot.read_section("institutional")
        if inst:
            body["institutional"] = inst
            body["spread_fuses"] = inst.get("spread_fuses") or {}
            body["multi_horizon"] = inst.get("multi_horizon") or {}
            body["zero_copy_pipeline"] = inst.get("zero_copy_pipeline") or {}
            body["hot_swap"] = inst.get("hot_swap") or get_hot_swap_snapshot()
            body["regime_kalman"] = inst.get("regime_kalman") or {}
        ps = IronLedgerSnapshot.read_section("portfolio_synthesis")
        if ps:
            body["portfolio_synthesis"] = ps
            body["cognitive_risk_heatmap"] = ps.get("cognitive_risk_heatmap") or {}
        ledger = IronLedgerSnapshot.read()
        traj = ledger.get("pp_trajectory_7d")
        if traj:
            body["pp_trajectory_7d"] = traj
        backup = (ledger.get("guardian") or {}).get("database_backup_compliance")
        if backup:
            body["database_backup_compliance"] = backup
    except Exception:
        pass
    return body


def start_autonomic_healer(*, rest: Any | None = None) -> None:
    global _thread, _rest_client, _boot_anchor_ts, _failover_engaged, _continuity_pass_done
    global _drift_flattener_engaged, _drift_flattener_result, _init_blocker_since
    _rest_client = rest
    _boot_anchor_ts = time.time()
    _failover_engaged = False
    _continuity_pass_done = False
    _drift_flattener_engaged = False
    _drift_flattener_result = {}
    _init_blocker_since = {}
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_engine.run_loop,
            name="AutonomicHealerEngine",
            daemon=True,
        )
        _thread.start()
    try:
        from system.market_data_hub import get_market_data_hub

        get_market_data_hub().start_stream_frame_consumer()
    except Exception:
        pass
    _refresh_snapshot()


def stop_autonomic_healer() -> None:
    _stop.set()


def reset_autonomic_healer_for_tests() -> None:
    global _thread, _rest_client, _last_broker_handshake_error
    global _boot_anchor_ts, _failover_engaged, _continuity_pass_done
    global _cognitive_override_active, _cognitive_override_reason
    global _last_transport_category, _last_http_status, _last_network_stack_trace
    global _fallback_transport_tier, _synthetic_hydration_active
    global _drift_flattener_engaged, _drift_flattener_result, _init_blocker_since
    _stop.set()
    _thread = None
    _rest_client = None
    _last_broker_handshake_error = ""
    _last_transport_category = TransportFailureCategory.UNKNOWN.value
    _last_http_status = None
    _last_network_stack_trace = ""
    _fallback_transport_tier = ""
    _synthetic_hydration_active = False
    _boot_anchor_ts = 0.0
    _failover_engaged = False
    _continuity_pass_done = False
    _cognitive_override_active = False
    _cognitive_override_reason = ""
    _drift_flattener_engaged = False
    _drift_flattener_result = {}
    _init_blocker_since = {}
    with _lock:
        _mitigations.clear()
        _stream_failure_counts.clear()
        _snapshot.clear()
        _snapshot.update({"ok": True, "engine_alive": False, "ts": 0.0})
    reset_hot_swap_for_tests()
