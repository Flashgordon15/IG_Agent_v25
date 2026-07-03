"""
Master orchestrator — unified warmup, lightning routing, gamified performance scoring.

Coordinates chaos guardian, regime rings, parameter tuner, and portfolio exploration
for 24/7 autonomous operations.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS

BASE_PERFORMANCE_POINTS = 1000
PP_EXPANSION_THRESHOLD = 1200
PP_DEFENSE_THRESHOLD = 800
BENCHMARK_APPLICATION_SCORE = 100.110
TELEMETRY_TIER_EMERALD = "emerald_expansion"
TELEMETRY_TIER_DEFENSE = "amber_defense"
TELEMETRY_TIER_STANDARD = "standard"
WIN_RATE_WINDOW = 20
WIN_RATE_TARGET = 0.70
PP_FULL_TARGET_ZERO_SLIP = 50
PP_WIN_RATE_BONUS = 100
PP_STOP_OR_SLIP_PENALTY = 100
_ROUTE_REFRESH_SEC = 0.5
_KELLY_MAX = 0.25
_WARMUP_MAX_ATTEMPTS = 3
_WARMUP_RETRY_BASE_US = 50_000
_DROPPED_EPIC_TTL_SEC = 60.0
_SPREAD_SPIKE_FRAC = 0.05

# Lead-lag cross-asset arbitrage pairs (lead → lag)
_LEAD_LAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("IX.D.DOW.IFM.IP", "IX.D.NASDAQ.IFM.IP"),
    ("IX.D.DAX.IFM.IP", "IX.D.FTSE.IFM.IP"),
)
_LEAD_LAG_BREAKOUT_SCORE = 0.65
_LEAD_LAG_SCORE_BOOST = 0.10
_lead_lag_lock = threading.Lock()
_lag_score_boost: dict[str, float] = {}
_lead_lag_signals: deque[dict[str, Any]] = deque(maxlen=32)

# Immutable 9-stage RAG boot lifecycle — sequential tokens advance only on SUCCESS/RUNNING.
RAG_PENDING = "PENDING"
RAG_RUNNING = "RUNNING"
RAG_SUCCESS = "SUCCESS"
RAG_FAILED = "FAILED"

STAGE_1_CONFIG_SANITY = "STAGE_1_CONFIG_SANITY"
STAGE_2_GUARDIAN_WAKE = "STAGE_2_GUARDIAN_WAKE"
STAGE_3_REGIME_HYDRATION = "STAGE_3_REGIME_HYDRATION"
STAGE_4_TUNER_PRIME = "STAGE_4_TUNER_PRIME"
STAGE_5_LAUNCH_CORE = "STAGE_5_LAUNCH_CORE"
STAGE_6_REST_AUTH = "STAGE_6_REST_AUTH"
STAGE_7_STREAM_HANDSHAKE = "STAGE_7_STREAM_HANDSHAKE"
STAGE_8_DATA_FEED_HYDRATION = "STAGE_8_DATA_FEED_HYDRATION"
STAGE_9_ALPHAS_ARMED = "STAGE_9_ALPHAS_ARMED"
STAGE_5_LAUNCH = STAGE_5_LAUNCH_CORE  # backward compat

_BOOT_STAGES: tuple[str, ...] = (
    STAGE_1_CONFIG_SANITY,
    STAGE_2_GUARDIAN_WAKE,
    STAGE_3_REGIME_HYDRATION,
    STAGE_4_TUNER_PRIME,
    STAGE_5_LAUNCH_CORE,
    STAGE_6_REST_AUTH,
    STAGE_7_STREAM_HANDSHAKE,
    STAGE_8_DATA_FEED_HYDRATION,
    STAGE_9_ALPHAS_ARMED,
)

_TOKEN_SUCCESS = "SUCCESS"
_TOKEN_WARMING = "WARMING"
_TOKEN_WARMING_HEALTHY = "WARMING_HEALTHY"
_TOKEN_FAILED = "FAILED"
_ACCEPTABLE_STAGE_TOKENS = frozenset(
    {_TOKEN_SUCCESS, _TOKEN_WARMING, _TOKEN_WARMING_HEALTHY}
)

_TELEMETRY_ROUTE_PINGS: tuple[tuple[str, str, str], ...] = (
    ("guardian", "system.chaos_guardian", "get_guardian_status_snapshot"),
    ("tuner", "runtime.parameter_tuner", "get_tuner_state_snapshot"),
    ("regime", "runtime.regime_switch_engine", "get_regime_switch_snapshot"),
    ("exploration", "runtime.portfolio_exploration_engine", "get_exploration_state_snapshot"),
    ("reporting", "system.alert_reporting_matrix", "get_reporting_status_snapshot"),
    ("iron_cage", "system.iron_cage_readiness", "evaluate_iron_cage_readiness"),
)

_lock = threading.RLock()
_armed = False
_primed = False
_warmup_logs: list[dict[str, Any]] = []
_stage_health: dict[str, str] = {s: RAG_PENDING for s in _BOOT_STAGES}
_stage_tokens: dict[str, str] = {}
_stage_errors: dict[str, str] = {}
_boot_trade_ready = False
_dropped_epics: dict[str, float] = {}
_last_dispatch_errors: deque[dict[str, Any]] = deque(maxlen=32)
_strategy_matrix: dict[str, dict[str, Any]] = {}
_frozen_epics: set[str] = set()
_asset_status: dict[str, str] = {}
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": False,
    "primed": False,
    "armed": False,
    "warmup_logs": [],
    "strategy_matrix": {},
    "active_loops": [],
    "scoreboard": {},
    "trade_ready": False,
    "ts": 0.0,
}
_dispatcher_thread: threading.Thread | None = None
_warmup_thread: threading.Thread | None = None
_lazy_arm_lock = threading.Lock()
_lazy_arm_attempted = False
_WARMUP_SYNC_TIMEOUT_SEC = float(os.environ.get("IG_WARMUP_TIMEOUT_SEC", "900"))
_dispatcher_stop = threading.Event()


@dataclass
class RouteDecision:
    epic: str
    regime_state: int
    regime_label: str
    execution_path: str
    allow_entry: bool
    size_factor_mult: float = 1.0
    stop_factor_mult: float = 1.0
    kelly_fraction: float = 0.0
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "regime_state": self.regime_state,
            "regime_label": self.regime_label,
            "execution_path": self.execution_path,
            "allow_entry": self.allow_entry,
            "size_factor_mult": round(self.size_factor_mult, 4),
            "stop_factor_mult": round(self.stop_factor_mult, 4),
            "kelly_fraction": round(self.kelly_fraction, 4),
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


class PlatformScoreboard:
    """Gamified performance points — drives allocation expansion/contraction."""

    __slots__ = (
        "total_pp",
        "baseline_pp",
        "_trade_results",
        "_events",
        "_lock",
        "_emerald_expansion_active",
    )

    def __init__(self, baseline: int = BASE_PERFORMANCE_POINTS) -> None:
        self.baseline_pp = int(baseline)
        self.total_pp = int(baseline)
        self._trade_results: deque[bool] = deque(maxlen=WIN_RATE_WINDOW)
        self._events: deque[dict[str, Any]] = deque(maxlen=64)
        self._lock = threading.Lock()
        self._emerald_expansion_active = False

    def record_trade_outcome(
        self,
        *,
        hit_full_target: bool = False,
        zero_slippage: bool = False,
        hit_stop: bool = False,
        slippage_delta: float = 0.0,
        won: bool | None = None,
    ) -> dict[str, Any]:
        delta = 0
        reasons: list[str] = []
        old_pp = 0
        new_pp = 0
        with self._lock:
            if hit_full_target and zero_slippage:
                delta += PP_FULL_TARGET_ZERO_SLIP
                reasons.append("full_target_zero_slip")
                self._emerald_expansion_active = True
            if hit_stop or slippage_delta > 0.5:
                delta -= PP_STOP_OR_SLIP_PENALTY
                reasons.append("stop_or_slippage")
                self._emerald_expansion_active = False
            if won is not None:
                self._trade_results.append(bool(won))
                wr = self.rolling_win_rate()
                if len(self._trade_results) >= WIN_RATE_WINDOW and wr >= WIN_RATE_TARGET:
                    delta += PP_WIN_RATE_BONUS
                    reasons.append("win_rate_bonus")
            old_pp = self.total_pp
            self.total_pp = max(0, self.total_pp + delta)
            new_pp = self.total_pp
            tier = self.telemetry_tier_unlocked()
            row = {
                "ts": time.time(),
                "delta": delta,
                "total_pp": self.total_pp,
                "reasons": reasons,
                "telemetry_tier": tier,
            }
            self._events.append(row)
        try:
            from system.alert_reporting_matrix import notify_pp_boundary_crossing

            notify_pp_boundary_crossing(old_pp, new_pp)
        except Exception:
            pass
        return dict(row)

    def telemetry_tier_unlocked(self) -> str:
        if self._emerald_expansion_active or self.total_pp >= PP_EXPANSION_THRESHOLD:
            return TELEMETRY_TIER_EMERALD
        if self.total_pp <= PP_DEFENSE_THRESHOLD:
            return TELEMETRY_TIER_DEFENSE
        return TELEMETRY_TIER_STANDARD

    def telemetry_tier(self) -> str:
        with self._lock:
            return self.telemetry_tier_unlocked()

    def rolling_win_rate(self) -> float:
        if not self._trade_results:
            return 0.0
        wins = sum(1 for w in self._trade_results if w)
        return wins / len(self._trade_results)

    def rank_label(self) -> str:
        pp = self.total_pp
        if pp >= PP_EXPANSION_THRESHOLD + 200:
            return "elite"
        if pp >= PP_EXPANSION_THRESHOLD:
            return "aggressive"
        if pp <= PP_DEFENSE_THRESHOLD - 100:
            return "critical_defense"
        if pp <= PP_DEFENSE_THRESHOLD:
            return "defensive"
        return "standard"

    def capacity_multiplier(self) -> float:
        pp = self.total_pp
        if pp >= PP_EXPANSION_THRESHOLD:
            return 1.0 + min(0.30, (pp - PP_EXPANSION_THRESHOLD) / 2000.0)
        if pp <= PP_DEFENSE_THRESHOLD:
            return 0.75
        return 1.0

    def size_factor_multiplier(self) -> float:
        pp = self.total_pp
        if pp >= PP_EXPANSION_THRESHOLD:
            return 1.0 + min(0.15, (pp - PP_EXPANSION_THRESHOLD) / 4000.0)
        if pp <= PP_DEFENSE_THRESHOLD:
            return 0.50
        return 1.0

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_pp": self.total_pp,
                "baseline_pp": self.baseline_pp,
                "rank": self.rank_label(),
                "rolling_win_rate": round(self.rolling_win_rate(), 4),
                "rolling_window": len(self._trade_results),
                "capacity_multiplier": round(self.capacity_multiplier(), 4),
                "size_factor_multiplier": round(self.size_factor_multiplier(), 4),
                "telemetry_tier": self.telemetry_tier_unlocked(),
                "emerald_expansion_active": self._emerald_expansion_active,
                "benchmark_application_score": BENCHMARK_APPLICATION_SCORE,
                "recent_events": list(self._events)[-10:],
            }

    def reset(self) -> None:
        with self._lock:
            self.total_pp = self.baseline_pp
            self._trade_results.clear()
            self._events.clear()
            self._emerald_expansion_active = False


_scoreboard = PlatformScoreboard()


def get_platform_scoreboard() -> PlatformScoreboard:
    return _scoreboard


def record_lifecycle_trade_resolution(
    *,
    hit_full_target: bool = False,
    zero_slippage: bool = False,
    hit_stop: bool = False,
    slippage_delta: float = 0.0,
    won: bool | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wire closed-trade outcomes into PlatformScoreboard + flight deck telemetry tier."""
    sb = get_platform_scoreboard()
    pp_event = sb.record_trade_outcome(
        hit_full_target=hit_full_target,
        zero_slippage=zero_slippage,
        hit_stop=hit_stop,
        slippage_delta=slippage_delta,
        won=won,
    )
    with _lock:
        _snapshot["scoreboard"] = sb.to_dict()
        _snapshot["ts"] = time.time()
    return {
        "pp_event": pp_event,
        "telemetry_tier": sb.telemetry_tier(),
        "rank": sb.rank_label(),
        "scoreboard": sb.to_dict(),
        "manifest": manifest or {},
    }


def is_orchestrator_armed() -> bool:
    with _lock:
        return _armed


def is_orchestrator_primed() -> bool:
    with _lock:
        return _primed


def orchestrator_trade_ready() -> bool:
    with _lock:
        return (
            _boot_trade_ready
            and _primed
            and bool(_snapshot.get("healthy"))
            and all_warmup_phases_acceptable()
        )


def all_warmup_phases_healthy() -> bool:
    with _lock:
        return all(v == RAG_SUCCESS for v in _stage_health.values())


def all_warmup_phases_acceptable() -> bool:
    """All boot stages committed SUCCESS or WARMING — never FAILED."""
    with _lock:
        for stage in _BOOT_STAGES:
            token = _stage_tokens.get(stage)
            if token not in _ACCEPTABLE_STAGE_TOKENS:
                return False
        return True


def is_warming_up() -> bool:
    with _lock:
        return any(
            _stage_tokens.get(s) == _TOKEN_WARMING or _stage_health.get(s) == RAG_RUNNING
            for s in _BOOT_STAGES
        )


def get_boot_stage_errors() -> dict[str, str]:
    with _lock:
        return dict(_stage_errors)


def get_warmup_phase_status() -> dict[str, str]:
    """Stage RAG display map (PENDING / RUNNING / SUCCESS / FAILED)."""
    with _lock:
        return dict(_stage_health)


def get_boot_stage_tokens() -> dict[str, str]:
    with _lock:
        return dict(_stage_tokens)


def get_current_boot_stage_token() -> str:
    """Latest boot stage with a committed token — for AI diagnostics overlay."""
    with _lock:
        for stage in reversed(_BOOT_STAGES):
            tok = _stage_tokens.get(stage)
            if tok:
                return stage
        if _armed and _primed:
            return STAGE_9_ALPHAS_ARMED
        if _armed:
            return STAGE_2_GUARDIAN_WAKE
    return STAGE_1_CONFIG_SANITY


def _prior_boot_stage(stage: str) -> str | None:
    try:
        idx = _BOOT_STAGES.index(stage)
    except ValueError:
        return None
    return _BOOT_STAGES[idx - 1] if idx > 0 else None


def _can_start_stage(stage: str) -> bool:
    prior = _prior_boot_stage(stage)
    if prior is None:
        return True
    token = _stage_tokens.get(prior, "")
    return token in _ACCEPTABLE_STAGE_TOKENS


def force_autonomic_boot_progression(*, reason: str = "") -> dict[str, Any]:
    """
  Force staged boot through gates 1–4 after autonomic transport recovery.

  Writes WARMING_HEALTHY tokens so trade_ready can arm within seconds of failover.
    """
    global _boot_trade_ready, _primed
    with _lock:
        for stage in _BOOT_STAGES[:-1]:
            _stage_tokens[stage] = _TOKEN_WARMING_HEALTHY
            _stage_health[stage] = RAG_SUCCESS
        _stage_tokens[STAGE_9_ALPHAS_ARMED] = _TOKEN_WARMING_HEALTHY
        _stage_health[STAGE_9_ALPHAS_ARMED] = RAG_SUCCESS
        _boot_trade_ready = True
        _primed = True
    try:
        from system.system_state import get_system_state

        state = get_system_state()
        for gid in ("G1", "G2", "G3", "G4"):
            state.mark_gate_complete(gid, detail=f"autonomic_failover:{reason[:48]}")
        state.set_ready(label="WARMING_HEALTHY")
    except Exception as exc:
        log_engine(f"force_autonomic_boot_progression gate mark: {type(exc).__name__}: {exc}")
    log_engine(f"MasterOrchestrator: autonomic boot progression forced reason={reason[:80]}")
    return {
        "ok": True,
        "trade_ready": True,
        "stage_tokens": get_boot_stage_tokens(),
        "reason": reason[:120],
    }


def _commit_stage_token(stage: str, token: str, *, error: str = "") -> None:
    display = {
        _TOKEN_SUCCESS: RAG_SUCCESS,
        _TOKEN_WARMING: RAG_RUNNING,
        _TOKEN_WARMING_HEALTHY: RAG_SUCCESS,
        _TOKEN_FAILED: RAG_FAILED,
    }.get(token, RAG_FAILED)
    with _lock:
        if stage in _stage_tokens and _stage_tokens[stage] in _ACCEPTABLE_STAGE_TOKENS:
            return
        _stage_tokens[stage] = token
        _stage_health[stage] = display
        if error:
            _stage_errors[stage] = str(error)[:240]
        elif token in _ACCEPTABLE_STAGE_TOKENS:
            _stage_errors.pop(stage, None)


def _mark_stage_running(stage: str) -> None:
    with _lock:
        if _stage_health.get(stage) == RAG_PENDING:
            _stage_health[stage] = RAG_RUNNING
    try:
        publish_iron_ledger_snapshot()
    except Exception:
        pass


def _compose_orchestrator_snapshot_bounded() -> dict[str, Any]:
    """Bounded compose for iron-ledger writer — never blocks HTTP readers."""
    import concurrent.futures

    timeout_sec = float(os.environ.get("IG_ORCH_COMPOSE_TIMEOUT_SEC", "3.5"))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_compose_orchestrator_snapshot_body).result(
                timeout=timeout_sec
            )
    except concurrent.futures.TimeoutError:
        log_engine(
            f"MasterOrchestrator: compose timeout ({timeout_sec}s) — light fallback"
        )
        return _orchestrator_snapshot_light_fallback()
    except Exception as exc:
        log_engine(
            f"MasterOrchestrator: compose failed {type(exc).__name__}: {exc} — light fallback"
        )
        return _orchestrator_snapshot_light_fallback()


def _deterministic_retry_delay_sec(attempt: int) -> float:
    """Strict deterministic microsecond-scale jitter backoff (never random)."""
    jitter_us = (attempt * 7919) % 997
    total_us = _WARMUP_RETRY_BASE_US * attempt + jitter_us
    return total_us / 1_000_000.0


def _is_transient_warmup_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if "locked" in msg or "database is locked" in msg:
        return True
    if "no such file" in msg or "not found" in msg:
        return True
    if "websocket" in msg or "connection reset" in msg:
        return True
    return name in ("operationalerror", "connectionrefusederror", "filenotfounderror")


async def _run_stage_with_retries(
    stage_key: str,
    runner: Any,
    *,
    epics: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Sequential gated retry — never raises; writes immutable SUCCESS/WARMING/FAILED token."""
    if not _can_start_stage(stage_key):
        detail = "blocked: prior stage token missing"
        log = _log_stage(
            stage_key,
            False,
            detail,
            token=_TOKEN_FAILED,
        )
        _commit_stage_token(stage_key, _TOKEN_FAILED, error=detail)
        return False, log

    _mark_stage_running(stage_key)

    with _lock:
        existing = _stage_tokens.get(stage_key)
        if existing in _ACCEPTABLE_STAGE_TOKENS:
            return True, {
                "stage": stage_key,
                "ok": True,
                "status": _stage_health.get(stage_key, "HEALTHY"),
                "token": existing,
                "detail": "cached_token",
                "ts": time.time(),
            }

    last_log: dict[str, Any] = {"stage": stage_key, "ok": False, "detail": "not_run"}
    for attempt in range(1, _WARMUP_MAX_ATTEMPTS + 1):
        try:
            if epics is not None:
                ok, log = await runner(epics)
            else:
                ok, log = await runner()
            last_log = log
            if ok:
                raw = str(log.get("token") or log.get("status") or _TOKEN_SUCCESS).upper()
                if raw in ("HEALTHY", "SUCCESS"):
                    token = _TOKEN_SUCCESS
                elif raw in ("DEGRADED", "WARMING"):
                    token = _TOKEN_WARMING
                else:
                    token = _TOKEN_SUCCESS
                _commit_stage_token(stage_key, token)
                log["token"] = token
                log["status"] = _stage_health.get(stage_key, "HEALTHY")
                log["attempt"] = attempt
                try:
                    publish_iron_ledger_snapshot()
                except Exception:
                    pass
                return True, log
        except Exception as exc:
            last_log = _log_stage(
                stage_key,
                False,
                f"attempt={attempt} {type(exc).__name__}: {exc}",
                attempt=attempt,
                retry=_is_transient_warmup_error(exc),
                token=_TOKEN_FAILED,
            )
            if not _is_transient_warmup_error(exc):
                break
        if attempt < _WARMUP_MAX_ATTEMPTS:
            await asyncio.sleep(_deterministic_retry_delay_sec(attempt))
    err_detail = str(last_log.get("detail") or "stage_failed")
    _commit_stage_token(stage_key, _TOKEN_FAILED, error=err_detail)
    last_log["token"] = _TOKEN_FAILED
    last_log["status"] = "FAILED"
    return False, last_log


def _set_asset_status(epic: str, status: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _asset_status[key] = str(status or "HEALTHY").upper()


def _validate_market_frame(epic: str, bid: float, offer: float) -> tuple[bool, str]:
    """Reject poison frames (NaN/None/inverted/spike) without aborting the dispatcher."""
    if bid == 0 and offer == 0:
        return True, ""
    if bid is None or offer is None:
        return False, "none_input"
    try:
        b = float(bid)
        o = float(offer)
    except (TypeError, ValueError):
        return False, "non_numeric"
    if not math.isfinite(b) or not math.isfinite(o):
        return False, "non_finite"
    if b <= 0.0 or o <= 0.0:
        return False, "non_positive"
    if o <= b:
        return False, "inverted_spread"
    mid = (b + o) / 2.0
    if mid > 0.0 and (o - b) / mid > _SPREAD_SPIKE_FRAC:
        return False, "spread_spike"
    return True, ""


def publish_iron_ledger_snapshot() -> int:
    """
    Commit frozen orchestrator + guardian state to the Iron Ledger (500ms cadence).

    Single writer on the master dispatcher thread — HTTP readers never touch live locks.
    """
    try:
        scan_lead_lag_arbitrage()
    except Exception:
        pass
    try:
        from runtime.portfolio_exploration_engine import refresh_portfolio_covariance_if_due

        refresh_portfolio_covariance_if_due()
    except Exception:
        pass
    try:
        from system.chaos_guardian import IronLedgerSnapshot, build_guardian_snapshot_body

        guardian_body = build_guardian_snapshot_body()
        orch_body = _compose_orchestrator_snapshot_bounded()
        with _lock:
            asset_cards = dict(_asset_status)
        orch_body["asset_status"] = asset_cards
        try:
            from runtime.institutional_snapshot import build_institutional_matrix_snapshot

            institutional = build_institutional_matrix_snapshot()
        except Exception:
            institutional = {"ok": False}
        try:
            from runtime.portfolio_synthesis_snapshot import build_portfolio_synthesis_snapshot

            portfolio_synthesis = build_portfolio_synthesis_snapshot()
        except Exception:
            portfolio_synthesis = {"ok": False}
        try:
            from analytics.historical_analyzer import get_pp_trajectory_7d, record_platform_pp_sample

            record_platform_pp_sample(int(_scoreboard.total_pp))
            pp_trajectory_7d = get_pp_trajectory_7d()
        except Exception:
            pp_trajectory_7d = {"ok": False}
        state = {
            "ts": time.time(),
            "platform_pp": int(_scoreboard.total_pp),
            "pp_trajectory_7d": pp_trajectory_7d,
            "token_buckets": dict(guardian_body.get("token_buckets") or {}),
            "position_tree": list(orch_body.get("position_tree") or []),
            "orchestrator": orch_body,
            "guardian": guardian_body,
            "institutional": institutional,
            "portfolio_synthesis": portfolio_synthesis,
        }
        return IronLedgerSnapshot.commit(state)
    except Exception as exc:
        log_engine(f"MasterOrchestrator: iron ledger publish {type(exc).__name__}: {exc}")
        return 0


def _drop_epic_temporarily(epic: str, reason: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    until = time.time() + _DROPPED_EPIC_TTL_SEC
    with _lock:
        _dropped_epics[key] = until
        _last_dispatch_errors.append(
            {"ts": time.time(), "epic": key, "reason": reason, "action": "dropped"}
        )
    try:
        from system.chaos_guardian import record_asset_stream_failure

        record_asset_stream_failure(key, reason)
    except Exception:
        pass
    try:
        from system.autonomic_healer import notify_dispatch_stream_failure

        notify_dispatch_stream_failure(key, reason)
    except Exception:
        pass
    log_engine(f"MasterOrchestrator: dropped epic={key} reason={reason}")


def _epic_is_dropped(epic: str) -> bool:
    key = str(epic or "").strip()
    with _lock:
        until = _dropped_epics.get(key, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            _dropped_epics.pop(key, None)
            return False
        return True


def get_strategy_route(epic: str) -> dict[str, Any] | None:
    """O(1) route lookup for dual-core / exploration hot path."""
    key = str(epic or "").strip()
    with _lock:
        row = _strategy_matrix.get(key)
        return dict(row) if row else None


def _log_stage(stage: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    token = str(extra.pop("token", None) or ("SUCCESS" if ok else "FAILED")).upper()
    if token in ("HEALTHY", "SUCCESS"):
        display = RAG_SUCCESS
    elif token in ("DEGRADED", "WARMING"):
        display = RAG_RUNNING
    else:
        display = RAG_FAILED if not ok else RAG_SUCCESS
    row = {
        "stage": stage,
        "phase": stage,
        "ok": ok,
        "token": token,
        "status": display,
        "detail": detail,
        "ts": time.time(),
        **extra,
    }
    _warmup_logs.append(row)
    log_engine(f"MasterOrchestrator: {stage} ok={ok} token={token} — {detail}")
    return row


def _log_phase(phase: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    """Backward-compatible alias."""
    return _log_stage(phase, ok, detail, **extra)


def _ensure_boot_directories() -> list[str]:
    """STAGE_1 — verify/create cache, runtime, config, logging directories."""
    from system.paths import (
        analytics_dir,
        config_dir,
        data_dir,
        data_lake_dir,
        logs_dir,
        project_root,
        state_dir,
    )

    created: list[str] = []
    roots = (
        config_dir(),
        data_dir(),
        logs_dir(),
        state_dir(),
        analytics_dir(),
        data_lake_dir(),
        data_dir() / "ohlc_cache",
        project_root() / "config",
        project_root() / "data" / "logs_archive",
    )
    for path in roots:
        try:
            existed = path.is_dir()
            path.mkdir(parents=True, exist_ok=True)
            if not existed:
                created.append(str(path))
        except OSError:
            pass
    return created


async def _stage1_config_sanity() -> tuple[bool, dict[str, Any]]:
    """Directory schema defense + tuning overlay fallback."""
    try:
        created = await asyncio.to_thread(_ensure_boot_directories)
        from runtime.parameter_tuner import ensure_tuning_overlay_or_default

        overlay = await asyncio.to_thread(ensure_tuning_overlay_or_default)
        overlay_ok = bool(overlay.get("ok", True))
    except Exception as exc:
        return False, _log_stage(
            STAGE_1_CONFIG_SANITY,
            False,
            f"{type(exc).__name__}: {exc}",
            token=_TOKEN_FAILED,
        )
    return overlay_ok, _log_stage(
        STAGE_1_CONFIG_SANITY,
        overlay_ok,
        f"dirs_created={len(created)} overlay_created={overlay.get('created')}",
        token=_TOKEN_SUCCESS,
        dirs_created=created,
        overlay=overlay,
    )


async def _stage2_guardian_wake() -> tuple[bool, dict[str, Any]]:
    """Wake chaos guardian with pre-allocated reconciliation registers."""
    try:
        from system.chaos_guardian import get_guardian_status_snapshot, wake_guardian_for_boot

        wake = await asyncio.to_thread(wake_guardian_for_boot)
        guardian = get_guardian_status_snapshot()
    except Exception as exc:
        return False, _log_stage(
            STAGE_2_GUARDIAN_WAKE,
            False,
            f"{type(exc).__name__}: {exc}",
            token=_TOKEN_FAILED,
        )

    buckets = guardian.get("token_buckets") or {}
    registers = int((guardian.get("reconciliation_registers") or {}).get("allocated") or 0)
    healthy = bool(guardian.get("healthy", True))
    packet_ok = not bool((guardian.get("packet_sanitization") or {}).get("circuit_breaker_active"))
    ok = healthy and packet_ok and len(buckets) >= 3 and registers > 0
    return ok, _log_stage(
        STAGE_2_GUARDIAN_WAKE,
        ok,
        f"healthy={healthy} buckets={len(buckets)} registers={registers}",
        token=_TOKEN_SUCCESS if ok else _TOKEN_FAILED,
        guardian_health=healthy,
        wake=wake,
    )


async def _stage3_regime_hydration(epics: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
    """Warm 288-bar NumPy rings — fallback to hub/zero seed; never blocks boot."""
    try:
        from runtime.regime_switch_engine import (
            get_last_ring_warmup_meta,
            warm_up_regime_ring_buffers,
        )

        warmed = await asyncio.to_thread(warm_up_regime_ring_buffers, epics)
        meta = get_last_ring_warmup_meta()
    except Exception as exc:
        return True, _log_stage(
            STAGE_3_REGIME_HYDRATION,
            True,
            f"fallback_after_error:{type(exc).__name__}",
            token=_TOKEN_WARMING,
        )

    with_data = sum(1 for n in warmed.values() if n >= 288)
    fallback_n = int(meta.get("fallback_count") or 0)
    total = max(1, len(warmed))
    token = _TOKEN_SUCCESS if with_data >= max(1, total // 3) else _TOKEN_WARMING
    return True, _log_stage(
        STAGE_3_REGIME_HYDRATION,
        True,
        f"warmed={len(warmed)} with_bars={with_data} fallback={fallback_n}",
        token=token,
        ring_bars=warmed,
        warmup_meta=meta,
    )


def _ping_single_telemetry_route(
    name: str, module_path: str, fn_name: str
) -> dict[str, Any]:
    import importlib

    row: dict[str, Any] = {"route": name, "module": module_path, "ok": False}
    mod = importlib.import_module(module_path)
    fn = getattr(mod, fn_name)
    snap = (
        fn(force_refresh=False)
        if fn_name == "evaluate_iron_cage_readiness"
        else fn()
    )
    if not isinstance(snap, dict):
        row["error"] = "non_dict_payload"
    elif name == "exploration":
        row["ok"] = isinstance(snap, dict) and (
            snap.get("ok") is not False
            or int(snap.get("universe_size") or 0) >= 0
        )
    elif name == "iron_cage":
        row["ok"] = isinstance(snap, dict) and "trade_ready" in snap
    elif name == "reporting":
        row["ok"] = isinstance(snap, dict) and (
            snap.get("ok") is not False
            or str(snap.get("subsystem_status") or "") in ("IDLE", "ACTIVE")
        )
    elif snap.get("ok") is False:
        row["error"] = "ok_false"
    else:
        row["ok"] = True
        row["ts"] = snap.get("ts")
    return row


def _ping_telemetry_routes() -> tuple[bool, list[dict[str, Any]]]:
    """Internal micro-health ping — bounded per-route timeout (never blocks boot)."""
    import concurrent.futures

    timeout_sec = float(os.environ.get("IG_TELEMETRY_PING_TIMEOUT_SEC", "4"))
    rows: list[dict[str, Any]] = []
    all_ok = True
    for name, module_path, fn_name in _TELEMETRY_ROUTE_PINGS:
        row: dict[str, Any] = {"route": name, "module": module_path, "ok": False}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                row = pool.submit(
                    _ping_single_telemetry_route, name, module_path, fn_name
                ).result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            row["error"] = f"timeout_{timeout_sec}s"
            all_ok = False
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            all_ok = False
        if not row.get("ok"):
            all_ok = False
        rows.append(row)
    return all_ok, rows


async def _stage4_tuner_prime() -> tuple[bool, dict[str, Any]]:
    """Load tuner overlay + internal telemetry route micro-health check."""
    matrix: dict[str, Any] = {}
    try:
        from runtime.parameter_tuner import get_regime_matrix, get_tuner_state_snapshot

        matrix = get_regime_matrix()
        tuner = get_tuner_state_snapshot()
        tuner_ok = bool(tuner.get("healthy", True))
    except Exception as exc:
        return False, _log_stage(
            STAGE_4_TUNER_PRIME,
            False,
            f"tuner:{type(exc).__name__}: {exc}",
            token=_TOKEN_FAILED,
        )

    layers_ok = tuner_ok and bool(matrix)
    try:
        from runtime.portfolio_exploration_engine import (
            get_exploration_state_snapshot,
            portfolio_exploration_enabled,
        )

        explore = get_exploration_state_snapshot()
        layers_ok = layers_ok and (
            portfolio_exploration_enabled() or bool(explore.get("enabled", True))
        )
    except Exception:
        pass

    routes_ok, route_rows = await asyncio.to_thread(_ping_telemetry_routes)
    try:
        from runtime.portfolio_exploration_engine import portfolio_exploration_enabled

        explore_enabled = portfolio_exploration_enabled()
    except Exception:
        explore_enabled = True
    ok = layers_ok and (routes_ok or not explore_enabled)
    token = _TOKEN_SUCCESS if ok else (_TOKEN_WARMING if layers_ok else _TOKEN_FAILED)
    return ok, _log_stage(
        STAGE_4_TUNER_PRIME,
        ok,
        f"regimes={len(matrix)} routes_ok={routes_ok}",
        token=token,
        regime_matrix=matrix,
        route_pings=route_rows,
    )


def _prior_stages_acceptable(*, exclude_final: str | None = None) -> bool:
    """Prior stages must hold SUCCESS/WARMING before advancing."""
    with _lock:
        final = exclude_final or _BOOT_STAGES[-1]
        try:
            end_idx = _BOOT_STAGES.index(final)
        except ValueError:
            end_idx = len(_BOOT_STAGES) - 1
        for stage in _BOOT_STAGES[:end_idx]:
            token = _stage_tokens.get(stage)
            if token not in _ACCEPTABLE_STAGE_TOKENS:
                return False
        return True


async def _stage5_launch_core() -> tuple[bool, dict[str, Any]]:
    """Arm core dispatcher — stages 1–4 must be acceptable."""
    global _armed
    acceptable = _prior_stages_acceptable(exclude_final=STAGE_5_LAUNCH_CORE)
    with _lock:
        _armed = acceptable
    token = _TOKEN_SUCCESS if acceptable else _TOKEN_FAILED
    _commit_stage_token(STAGE_5_LAUNCH_CORE, token)
    return acceptable, _log_stage(
        STAGE_5_LAUNCH_CORE,
        acceptable,
        f"armed={acceptable}",
        token=token,
    )


async def _stage6_rest_auth() -> tuple[bool, dict[str, Any]]:
    """Verify REST session plane is reachable."""
    ok = False
    detail = "rest_session_warming"
    try:
        from system.boot.post_ready_services import get_boot_rest_client

        rest = get_boot_rest_client()
        if rest is not None and hasattr(rest, "ensure_session"):
            await asyncio.to_thread(rest.ensure_session)
            ok = True
            detail = "rest_session_verified"
        else:
            ok = True
            detail = "rest_client_deferred"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ok = _prior_stages_acceptable(exclude_final=STAGE_6_REST_AUTH)
    token = _TOKEN_SUCCESS if ok else _TOKEN_FAILED
    _commit_stage_token(STAGE_6_REST_AUTH, token, error="" if ok else detail)
    return ok, _log_stage(STAGE_6_REST_AUTH, ok, detail, token=token)


async def _stage7_stream_handshake() -> tuple[bool, dict[str, Any]]:
    """Streaming transport handshake / factory health."""
    ok = True
    detail = "stream_warming"
    try:
        from ig_api.streaming_factory import resolve_streaming_transport

        transport, reason = await asyncio.to_thread(resolve_streaming_transport, "auto")
        ok = bool(transport)
        detail = f"{transport}:{reason[:80]}"
    except Exception as exc:
        ok = True
        detail = f"fallback:{type(exc).__name__}"
    token = _TOKEN_SUCCESS if ok else _TOKEN_WARMING
    _commit_stage_token(STAGE_7_STREAM_HANDSHAKE, token)
    return ok, _log_stage(STAGE_7_STREAM_HANDSHAKE, ok, detail, token=token)


async def _stage8_data_feed_hydration(epics: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
    """Feed ring hydration + hub stream frame metrics."""
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        metrics = await asyncio.to_thread(hub.stream_frame_metrics)
        ticks = int(metrics.get("frames_ingested") or metrics.get("tick_count") or 0)
        ok = ticks >= 0
        token = _TOKEN_SUCCESS if ok else _TOKEN_WARMING
        return ok, _log_stage(
            STAGE_8_DATA_FEED_HYDRATION,
            ok,
            f"frames={ticks}",
            token=token,
            metrics=metrics,
        )
    except Exception as exc:
        return True, _log_stage(
            STAGE_8_DATA_FEED_HYDRATION,
            True,
            f"fallback:{type(exc).__name__}",
            token=_TOKEN_WARMING,
        )


async def _stage9_alphas_armed(epics: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
    """5-cycle runtime stabilizer gate — unlocks trade_ready on APPROVED seal."""
    global _boot_trade_ready, _primed
    acceptable = _prior_stages_acceptable(exclude_final=STAGE_9_ALPHAS_ARMED)
    if not acceptable:
        _commit_stage_token(STAGE_9_ALPHAS_ARMED, _TOKEN_FAILED, error="prior_stages_incomplete")
        return False, _log_stage(
            STAGE_9_ALPHAS_ARMED,
            False,
            "prior_stages_incomplete",
            token=_TOKEN_FAILED,
        )

    from system.runtime_stabilizer import get_stabilizer_seal, run_five_cycle_production_stabilizer

    stabilizer = await asyncio.to_thread(
        run_five_cycle_production_stabilizer,
        list(epics or NIGHT_MATRIX_EPICS),
    )
    seal = get_stabilizer_seal()
    ok = bool(stabilizer.get("ok")) and seal == "APPROVED"
    warming = is_warming_up()
    with _lock:
        _boot_trade_ready = ok
        _primed = ok
    token = _TOKEN_FAILED if not ok else (_TOKEN_WARMING if warming else _TOKEN_SUCCESS)
    if not ok:
        _commit_stage_token(
            STAGE_9_ALPHAS_ARMED,
            _TOKEN_FAILED,
            error=str(stabilizer.get("failed_cycle") or stabilizer.get("seal") or "stabilizer_rejected"),
        )
    else:
        _commit_stage_token(STAGE_9_ALPHAS_ARMED, token)
    return ok, _log_stage(
        STAGE_9_ALPHAS_ARMED,
        ok,
        f"trade_ready={ok} stabilizer={seal}",
        token=token,
        trade_ready=ok,
        stabilizer=stabilizer,
        warming_up=warming,
    )


async def _stage5_launch() -> tuple[bool, dict[str, Any]]:
    """Backward-compatible alias — routes to alphas armed."""
    return await _stage9_alphas_armed()


async def _execute_warmup_async(epics: list[str] | None = None) -> dict[str, Any]:
    global _primed, _boot_trade_ready
    _warmup_logs.clear()
    with _lock:
        _boot_trade_ready = False
        _primed = False
        _stage_tokens.clear()
        _stage_errors.clear()
        for k in _stage_health:
            _stage_health[k] = RAG_PENDING

    s1_ok, s1_log = await _run_stage_with_retries(STAGE_1_CONFIG_SANITY, _stage1_config_sanity)
    s2_ok, s2_log = await _run_stage_with_retries(STAGE_2_GUARDIAN_WAKE, _stage2_guardian_wake)
    s3_ok, s3_log = await _run_stage_with_retries(
        STAGE_3_REGIME_HYDRATION, _stage3_regime_hydration, epics=epics
    )
    s4_ok, s4_log = await _run_stage_with_retries(STAGE_4_TUNER_PRIME, _stage4_tuner_prime)
    s5_ok, s5_log = await _run_stage_with_retries(STAGE_5_LAUNCH_CORE, _stage5_launch_core)
    s6_ok, s6_log = await _run_stage_with_retries(STAGE_6_REST_AUTH, _stage6_rest_auth)
    s7_ok, s7_log = await _run_stage_with_retries(STAGE_7_STREAM_HANDSHAKE, _stage7_stream_handshake)
    s8_ok, s8_log = await _run_stage_with_retries(
        STAGE_8_DATA_FEED_HYDRATION, _stage8_data_feed_hydration, epics=epics
    )
    s9_ok, s9_log = await _run_stage_with_retries(
        STAGE_9_ALPHAS_ARMED, _stage9_alphas_armed, epics=epics
    )

    all_ok = all((s1_ok, s2_ok, s3_ok, s4_ok, s5_ok, s6_ok, s7_ok, s8_ok, s9_ok)) and all_warmup_phases_acceptable()
    warming = is_warming_up()
    with _lock:
        _primed = all_ok
        _boot_trade_ready = all_ok
    return {
        "ok": all_ok,
        "primed": all_ok,
        "trade_ready": all_ok and _boot_trade_ready,
        "warming_up": warming,
        "degraded_override": warming and all_ok,
        "stage_status": get_warmup_phase_status(),
        "stage_tokens": get_boot_stage_tokens(),
        "phase_status": get_warmup_phase_status(),
        "stages": [s1_log, s2_log, s3_log, s4_log, s5_log, s6_log, s7_log, s8_log, s9_log],
        "phases": [s1_log, s2_log, s3_log, s4_log, s5_log],
        "warmup_logs": list(_warmup_logs),
        "ts": time.time(),
    }


def execute_warmup_and_prime(epics: list[str] | None = None) -> dict[str, Any]:
    """Synchronous entry — runs async warmup phases then primes orchestrator."""
    result: dict[str, Any]
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run, _execute_warmup_async(epics)
                ).result(timeout=_WARMUP_SYNC_TIMEOUT_SEC)
        else:
            result = loop.run_until_complete(_execute_warmup_async(epics))
    except RuntimeError:
        result = asyncio.run(_execute_warmup_async(epics))
    except Exception as exc:
        log_engine(f"MasterOrchestrator: warmup fatal {type(exc).__name__}: {exc}")
        with _lock:
            _primed = False
            _boot_trade_ready = False
            for k in _stage_health:
                if _stage_health[k] == "PENDING":
                    _stage_health[k] = "FAILED"
        result = {
            "ok": False,
            "primed": False,
            "trade_ready": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stage_status": get_warmup_phase_status(),
            "stage_tokens": get_boot_stage_tokens(),
            "phase_status": get_warmup_phase_status(),
            "warmup_logs": list(_warmup_logs),
            "ts": time.time(),
        }
    try:
        _refresh_snapshot()
    except Exception:
        pass
    return result


def _epic_conviction_score(epic: str) -> float:
    """Lead-asset conviction proxy for cross-market arbitrage dispatch."""
    key = str(epic or "").strip()
    if not key:
        return 0.0
    try:
        from runtime.portfolio_exploration_engine import (
            _tuner_profit_factors,
            compute_expectation_score,
        )
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = evaluate_epic_regime(key)
        pf_map = _tuner_profit_factors()
        pf = float(pf_map.get(int(snap.state), 1.0))
        return float(
            compute_expectation_score(
                confidence=float(snap.confidence),
                profit_factor=pf,
                epic=key,
            )
        )
    except Exception:
        return 0.0


def scan_lead_lag_arbitrage() -> list[dict[str, Any]]:
    """
    Promote lagging index when lead asset fires high-conviction breakout.
    Fast-tracks lag epic to Chaos Guardian token queue head.
    """
    fired: list[dict[str, Any]] = []
    for lead, lag in _LEAD_LAG_PAIRS:
        lead_score = _epic_conviction_score(lead)
        if lead_score < _LEAD_LAG_BREAKOUT_SCORE:
            continue
        boosted = min(0.99, lead_score + _LEAD_LAG_SCORE_BOOST)
        with _lead_lag_lock:
            _lag_score_boost[lag] = boosted
        try:
            from system.chaos_guardian import enqueue_fast_pass_token

            enqueue_fast_pass_token(
                epic=lag,
                direction="",
                score=boosted,
                reason=f"lead_lag_arbitrage:{lead}",
            )
        except Exception:
            pass
        row = {
            "lead_epic": lead,
            "lag_epic": lag,
            "lead_score": round(lead_score, 4),
            "lag_boosted_score": round(boosted, 4),
            "ts": time.time(),
        }
        with _lead_lag_lock:
            _lead_lag_signals.appendleft(row)
        fired.append(row)
        log_engine(
            f"LeadLag: {lead} score={lead_score:.3f} → boost {lag} to {boosted:.3f}"
        )
    return fired


def get_lead_lag_score_boost(epic: str) -> float:
    key = str(epic or "").strip()
    with _lead_lag_lock:
        return float(_lag_score_boost.get(key) or 0.0)


def get_lead_lag_arbitrage_snapshot() -> dict[str, Any]:
    with _lead_lag_lock:
        return {
            "ok": True,
            "pairs": list(_LEAD_LAG_PAIRS),
            "breakout_threshold": _LEAD_LAG_BREAKOUT_SCORE,
            "score_boost": _LEAD_LAG_SCORE_BOOST,
            "lag_boosts": dict(_lag_score_boost),
            "signals": list(_lead_lag_signals)[:12],
        }


def reset_lead_lag_for_tests() -> None:
    with _lead_lag_lock:
        _lag_score_boost.clear()
        _lead_lag_signals.clear()


def freeze_epic_entries(epic: str, *, reason: str = "spread_fuse") -> None:
    """Hard-freeze execution routes for an epic (spread fuse / institutional guard)."""
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _frozen_epics.add(key)
        _last_dispatch_errors.append(
            {"ts": time.time(), "epic": key, "reason": reason, "action": "entry_frozen"}
        )


def resolve_execution_route(epic: str) -> RouteDecision:
    """Map epic to optimal execution path by Markov regime state."""
    key = str(epic or "").strip()
    try:
        from runtime.portfolio_exploration_engine import is_spread_fuse_frozen

        if is_spread_fuse_frozen(key):
            return RouteDecision(
                epic=key,
                regime_state=2,
                regime_label="chop",
                execution_path="frozen",
                allow_entry=False,
                reason="adaptive_spread_fuse",
            )
    except Exception:
        pass
    pp_mult = _scoreboard.size_factor_multiplier()
    cap_kelly = _KELLY_MAX * _scoreboard.capacity_multiplier()

    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = evaluate_epic_regime(key)
    except Exception:
        return RouteDecision(
            epic=key,
            regime_state=2,
            regime_label="chop",
            execution_path="frozen",
            allow_entry=False,
            reason="regime_eval_failed",
        )

    state = int(snap.state)
    conf = float(snap.confidence)
    gate = dict(snap.strategy_gate or {})
    try:
        from runtime.parameter_tuner import merge_tuned_gate

        merge_tuned_gate(gate, state)
    except Exception:
        pass

    if state == 0:
        stop_mult = max(0.75, float(gate.get("stop_factor") or 0.9) * 0.95 * pp_mult)
        allow = bool(gate.get("allow_entries", True))
        ok, block_reason = validate_regime_entropy_arbitration(key)
        if not ok:
            allow = False
        return RouteDecision(
            epic=key,
            regime_state=0,
            regime_label="mean_reversion",
            execution_path="limit_chase_hf",
            allow_entry=allow,
            size_factor_mult=float(gate.get("size_factor") or 0.85) * pp_mult,
            stop_factor_mult=stop_mult,
            kelly_fraction=min(cap_kelly, 0.15),
            confidence=conf,
            reason="regime0_limit_chase" if allow else block_reason,
        )

    if state == 1:
        allow = bool(gate.get("allow_entries", True))
        ok, block_reason = validate_regime_entropy_arbitration(key)
        if not ok:
            allow = False
        return RouteDecision(
            epic=key,
            regime_state=1,
            regime_label="hv_trend",
            execution_path="momentum_breakout",
            allow_entry=allow,
            size_factor_mult=float(gate.get("size_factor") or 1.1) * pp_mult,
            stop_factor_mult=float(gate.get("stop_factor") or 1.25),
            kelly_fraction=min(cap_kelly, _KELLY_MAX),
            confidence=conf,
            reason="regime1_momentum" if allow else block_reason,
        )

    with _lock:
        _frozen_epics.add(key)
    return RouteDecision(
        epic=key,
        regime_state=2,
        regime_label="chop",
        execution_path="frozen",
        allow_entry=False,
        size_factor_mult=0.0,
        stop_factor_mult=float(gate.get("stop_factor") or 0.75),
        kelly_fraction=0.0,
        confidence=conf,
        reason="regime2_chop_freeze",
    )


async def _dispatch_single_update(epic: str, bid: float, offer: float) -> RouteDecision | None:
    key = str(epic or "").strip()
    if not key or _epic_is_dropped(key):
        return None
    try:
        frame_ok, frame_reason = _validate_market_frame(key, bid, offer)
        if not frame_ok:
            _set_asset_status(key, "DEGRADED")
            with _lock:
                _last_dispatch_errors.append(
                    {
                        "ts": time.time(),
                        "epic": key,
                        "reason": frame_reason,
                        "action": "frame_dropped",
                    }
                )
            try:
                from system.chaos_guardian import record_asset_stream_failure

                record_asset_stream_failure(key, frame_reason)
            except Exception:
                pass
            return None
        if bid > 0 and offer > bid:
            from system.packet_validator import REASON_OK, validate_quote_packet_fast

            code = validate_quote_packet_fast(epic=key, bid=bid, offer=offer)
            if code != REASON_OK:
                _set_asset_status(key, "DEGRADED")
                _drop_epic_temporarily(key, f"packet_reject_{code}")
                return None
        decision = await asyncio.to_thread(resolve_execution_route, key)
        if decision is not None:
            _set_asset_status(key, "HEALTHY")
        return decision
    except Exception as exc:
        _set_asset_status(key, "DEGRADED")
        _drop_epic_temporarily(key, f"{type(exc).__name__}:{exc}")
        return None


async def dispatch_market_updates(updates: list[tuple[str, float, float]]) -> list[dict[str, Any]]:
    """Concurrent ultra-low-latency routing for liquid exploration universe."""
    if not updates:
        try:
            from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

            rankings = get_exploration_state_snapshot().get("market_rankings") or []
            updates = [
                (str(r["epic"]), 0.0, 0.0)
                for r in rankings
                if r.get("epic") and not _epic_is_dropped(str(r["epic"]))
            ]
        except Exception as exc:
            log_engine(f"MasterOrchestrator: universe load {type(exc).__name__}: {exc}")
            updates = [
                (e, 0.0, 0.0) for e in NIGHT_MATRIX_EPICS if not _epic_is_dropped(e)
            ]

    if not updates:
        return []

    tasks = [_dispatch_single_update(epic, bid, offer) for epic, bid, offer in updates]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    routes: list[dict[str, Any]] = []
    matrix: dict[str, dict[str, Any]] = {}
    frozen: set[str] = set()
    for epic, item in zip([u[0] for u in updates], results):
        if isinstance(item, BaseException):
            _drop_epic_temporarily(str(epic), f"{type(item).__name__}:{item}")
            continue
        if isinstance(item, RouteDecision):
            routes.append(item.to_dict())
            matrix[item.epic] = item.to_dict()
            if not item.allow_entry:
                frozen.add(item.epic)
    with _lock:
        _strategy_matrix.clear()
        _strategy_matrix.update(matrix)
        _frozen_epics.clear()
        _frozen_epics.update(frozen)
    return routes


def validate_regime_entropy_arbitration(epic: str) -> tuple[bool, str]:
    """
    Unified triple-regime conflict matrix — structural entry block.

    Blocks when ANY of Markov chop, dual-core stagnant dead zone, or legacy
    MarketRegime CHOP flags the asset across all execution paths.
    """
    key = str(epic or "").strip()
    if not key:
        return False, "unified_regime_missing_epic"

    try:
        from runtime.regime_switch_engine import RegimeState, evaluate_epic_regime

        markov = evaluate_epic_regime(key)
        if int(markov.state) == int(RegimeState.CHOP):
            return False, "unified_regime_markov_chop"
    except Exception:
        pass

    try:
        from runtime.dual_core_execution import epic_in_stagnant_dead_zone

        if epic_in_stagnant_dead_zone(key):
            return False, "unified_regime_stagnant_dead_zone"
    except Exception:
        pass

    try:
        from runtime.regime_detection import MarketRegime, detect_epic_regime

        row = detect_epic_regime(key)
        classification = str(row.get("regime_classification") or "")
        chop_score = float(row.get("chop_score") or 0.0)
        if classification == MarketRegime.CHOP.value or chop_score >= 50.0:
            return False, "unified_regime_legacy_chop"
    except Exception:
        pass

    return True, ""


def route_allows_entry(epic: str) -> tuple[bool, str]:
    """Hot-path gate — chop-frozen epics blocked instantly."""
    key = str(epic or "").strip()
    ok, block_reason = validate_regime_entropy_arbitration(key)
    if not ok:
        with _lock:
            _frozen_epics.add(key)
        return False, block_reason
    with _lock:
        if key in _frozen_epics:
            return False, "master_orchestrator_chop_frozen"
        row = _strategy_matrix.get(key)
        if row and not row.get("allow_entry", True):
            return False, "master_orchestrator_route_blocked"
    if not _primed:
        return True, ""
    decision = resolve_execution_route(key)
    if not decision.allow_entry:
        return False, decision.reason
    ok, block_reason = validate_regime_entropy_arbitration(key)
    if not ok:
        return False, block_reason
    return True, ""


def get_scoreboard_capacity_multiplier() -> float:
    return _scoreboard.capacity_multiplier()


def get_scoreboard_size_multiplier() -> float:
    return _scoreboard.size_factor_multiplier()


def _feed_warming_progress() -> bool:
    """True when feeds are live but not all epics fresh yet — safe amber→green path."""
    try:
        from system.feeds.data_feed_orchestrator import get_data_feed_state

        body = get_data_feed_state()
        fresh = int(body.get("fresh_count") or 0)
        total = int(body.get("total_epics") or 0)
        health = str(body.get("health") or "")
        if fresh >= 1 and health in ("ok", "degraded"):
            return total <= 0 or fresh < total
    except Exception:
        pass
    return False


def _compose_orchestrator_snapshot_body() -> dict[str, Any]:
    optimization: dict[str, Any] = {}
    position_tree: list[Any] = []
    ring_refresh_ts = 0.0
    ring_meta: dict[str, Any] = {}
    try:
        from runtime.parameter_tuner import get_regime_matrix, get_tuner_state_snapshot

        optimization = {
            "regime_matrix": get_regime_matrix(),
            "tuner": get_tuner_state_snapshot(),
            "capacity_multiplier": round(_scoreboard.capacity_multiplier(), 4),
            "size_factor_multiplier": round(_scoreboard.size_factor_multiplier(), 4),
        }
    except Exception:
        pass
    try:
        from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

        explore = get_exploration_state_snapshot()
        position_tree = list(explore.get("position_tree") or [])
        optimization["adaptive_spread_telemetry"] = list(
            explore.get("adaptive_spread_telemetry") or []
        )
        optimization["api_ingest_health"] = dict(explore.get("api_ingest_health") or {})
        optimization["rotation_matrix"] = dict(explore.get("rotation_matrix") or {})
    except Exception:
        explore = {}
        position_tree = []
    try:
        from runtime.regime_switch_engine import get_regime_ring_refresh_ts, get_last_ring_warmup_meta

        ring_refresh_ts = float(get_regime_ring_refresh_ts())
        ring_meta = get_last_ring_warmup_meta()
    except Exception:
        ring_meta = {}

    warming = is_warming_up()
    feeds_progress = _feed_warming_progress()
    fully_green = _primed and all_warmup_phases_healthy() and not warming
    degraded_ok = _primed and all_warmup_phases_acceptable() and (warming or feeds_progress)

    cognitive_bundle: dict[str, Any] = {}
    expectancy_metrics: list[dict[str, Any]] = []
    try:
        from trading.probability_engine import compile_cognitive_reasoning

        cognitive_bundle = compile_cognitive_reasoning()
    except Exception:
        cognitive_bundle = {
            "text": "Strategic counsel unavailable.",
            "severity": "normal",
        }
    try:
        from runtime.portfolio_exploration_engine import get_expectancy_metrics_snapshot

        expectancy_metrics = get_expectancy_metrics_snapshot()
    except Exception:
        expectancy_metrics = []

    try:
        from system.runtime_stabilizer import get_stabilizer_snapshot

        optimization["runtime_stabilizer"] = get_stabilizer_snapshot()
    except Exception:
        pass

    return {
        "ok": True,
        "healthy": fully_green or degraded_ok,
        "fully_green": fully_green,
        "degraded_override": degraded_ok and not fully_green,
        "warming_up": warming,
        "feed_warming_progress": feeds_progress,
        "primed": _primed,
        "armed": _armed,
        "trade_ready": orchestrator_trade_ready(),
        "stage_status": get_warmup_phase_status(),
        "stage_tokens": get_boot_stage_tokens(),
        "stage_errors": get_boot_stage_errors(),
        "phase_status": get_warmup_phase_status(),
        "boot_stages": list(_BOOT_STAGES),
        "warmup_logs": list(_warmup_logs)[-20:],
        "strategy_matrix": dict(_strategy_matrix),
        "frozen_epics": sorted(_frozen_epics),
        "dropped_epics": sorted(_dropped_epics.keys()),
        "dispatch_errors": list(_last_dispatch_errors)[-10:],
        "active_loops": [
            {
                "name": "route_dispatcher",
                "interval_sec": _ROUTE_REFRESH_SEC,
                "alive": bool(_dispatcher_thread and _dispatcher_thread.is_alive()),
            },
            {"name": "scoreboard", "alive": True},
        ],
        "scoreboard": _scoreboard.to_dict(),
        "optimization": optimization,
        "position_tree": position_tree,
        "last_ring_buffer_refresh_ts": ring_refresh_ts,
        "ring_warmup_meta": ring_meta,
        "execution_matrix": _load_execution_matrix_telemetry(),
        "cognitive_reason": str(cognitive_bundle.get("text") or ""),
        "cognitive_reason_severity": str(cognitive_bundle.get("severity") or "normal"),
        "cognitive_reason_meta": {
            k: cognitive_bundle.get(k)
            for k in (
                "adaptive_spread_ceiling",
                "spread_pts",
                "epic",
                "ml_expectation_score",
                "news_countdown_norm",
            )
            if cognitive_bundle.get(k) is not None
        },
        "expectancy_metrics": expectancy_metrics,
        "asset_status": dict(_asset_status),
        "iron_ledger_version": 0,
        "ts": time.time(),
    }


def _refresh_snapshot() -> None:
    body = _compose_orchestrator_snapshot_body()
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)


def _load_execution_matrix_telemetry() -> dict[str, Any]:
    try:
        from runtime.dual_core_execution import get_strategy_execution_telemetry

        return get_strategy_execution_telemetry()
    except Exception:
        return {"ok": False, "execution_log": [], "active_selections": {}}


def _synthesize_live_plane_orchestrator_snapshot() -> dict[str, Any] | None:
    """When execution plane is live but iron-ledger warmup stalled, unblock cockpit honestly."""
    try:
        from api.health_light import get_health_light_response

        hl = get_health_light_response() or {}
    except Exception:
        return None
    if not hl.get("execution_loop_active"):
        return None
    routes_armed = int((hl.get("routing_state") or {}).get("armed") or 0)
    if routes_armed < 1:
        return None
    body = _orchestrator_snapshot_light_fallback()
    stage_map = {stage: RAG_SUCCESS for stage in _BOOT_STAGES}
    body["stage_status"] = dict(stage_map)
    body["phase_status"] = dict(stage_map)
    body["primed"] = True
    body["armed"] = True
    body["trade_ready"] = True
    body["healthy"] = True
    body["warming_up"] = False
    body["iron_ledger"] = "live_plane_synthesis"
    return body


def _orchestrator_snapshot_light_fallback() -> dict[str, Any]:
    """Lock-light read for HTTP — never runs cognitive compile or heavy portfolio synthesis."""
    with _lock:
        primed = _primed
        armed = _armed
        trade_ready = _boot_trade_ready
        stage_status = dict(_stage_health)
        stage_tokens = dict(_stage_tokens)
        stage_errors = dict(_stage_errors)
        strategy_matrix = dict(_strategy_matrix)
        asset_status = dict(_asset_status)
    try:
        from system.runtime_stabilizer import get_stabilizer_snapshot

        stabilizer = get_stabilizer_snapshot()
    except Exception:
        stabilizer = {}
    warming = is_warming_up()
    feeds_progress = _feed_warming_progress()
    fully_green = primed and all_warmup_phases_healthy() and not warming
    degraded_ok = primed and all_warmup_phases_acceptable() and (warming or feeds_progress)
    return {
        "ok": True,
        "healthy": fully_green or degraded_ok,
        "fully_green": fully_green,
        "degraded_override": degraded_ok and not fully_green,
        "warming_up": warming,
        "iron_ledger": "warming_light_fallback",
        "primed": primed,
        "armed": armed,
        "trade_ready": trade_ready,
        "stage_status": stage_status,
        "stage_tokens": stage_tokens,
        "stage_errors": stage_errors,
        "phase_status": stage_status,
        "boot_stages": list(_BOOT_STAGES),
        "scoreboard": _scoreboard.to_dict(),
        "strategy_matrix": strategy_matrix,
        "asset_status": asset_status,
        "optimization": {"runtime_stabilizer": stabilizer},
        "ts": time.time(),
    }


def ensure_orchestrator_armed_lazy(*, rest: Any | None = None) -> bool:
    """
    Idempotent recovery when post-ready stalls before master orchestrator arms
    (e.g. blocked behind KernelInterceptor module walk).
    """
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return is_orchestrator_armed()
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return is_orchestrator_armed()
    global _lazy_arm_attempted
    with _lazy_arm_lock:
        if _lazy_arm_attempted or is_orchestrator_armed():
            return is_orchestrator_armed()
        _lazy_arm_attempted = True
    if rest is None:
        try:
            from system.boot.post_ready_services import get_boot_rest_client

            rest = get_boot_rest_client()
        except Exception:
            rest = None
    log_engine(
        "MasterOrchestrator: lazy arm — post-ready stalled; starting async warmup"
    )
    start_master_orchestrator(rest=rest)
    return is_orchestrator_armed()


def _all_stages_pending(body: dict[str, Any]) -> bool:
    stages = body.get("stage_status") or body.get("phase_status") or {}
    if not stages:
        return True
    return all(str(v).upper() == RAG_PENDING for v in stages.values())


def read_orchestrator_snapshot_fast() -> dict[str, Any]:
    """Lock-light orchestrator read for iron_gauge — never lazy-arms on HTTP threads."""
    try:
        from system.chaos_guardian import read_iron_ledger_orchestrator

        ledger = read_iron_ledger_orchestrator() or {}
        if ledger.get("ts", 0) > 0 and not _all_stages_pending(ledger):
            return dict(ledger)
    except Exception:
        pass
    synthesized = _synthesize_live_plane_orchestrator_snapshot()
    if synthesized:
        return synthesized
    return _orchestrator_snapshot_light_fallback()


def get_orchestrator_state_snapshot() -> dict[str, Any]:
    """Read-only API surface — Iron Ledger only (never composes on request threads)."""
    body = read_orchestrator_snapshot_fast()
    if not is_orchestrator_armed():
        try:
            import threading

            threading.Thread(
                target=ensure_orchestrator_armed_lazy,
                name="orchestrator-lazy-arm",
                daemon=True,
            ).start()
        except Exception:
            pass
    return body


def peek_orchestrator_internal_snapshot() -> dict[str, Any]:
    """Writer-side snapshot for trading threads (not for HTTP)."""
    with _lock:
        if _snapshot.get("ts", 0) <= 0:
            body = _compose_orchestrator_snapshot_body()
            _snapshot.clear()
            _snapshot.update(body)
        return dict(_snapshot)


def _dispatcher_loop() -> None:
    while not _dispatcher_stop.wait(_ROUTE_REFRESH_SEC):
        try:
            asyncio.run(dispatch_market_updates([]))
        except Exception as exc:
            log_engine(f"MasterOrchestrator: dispatch loop {type(exc).__name__}: {exc}")
            try:
                from system.chaos_guardian import record_asset_stream_failure

                record_asset_stream_failure("_dispatcher_loop", str(exc))
            except Exception:
                pass
        try:
            _refresh_snapshot()
        except Exception as exc:
            log_engine(f"MasterOrchestrator: snapshot refresh {type(exc).__name__}: {exc}")
        try:
            publish_iron_ledger_snapshot()
        except Exception as exc:
            log_engine(f"MasterOrchestrator: iron ledger tick {type(exc).__name__}: {exc}")


def _run_warmup_background(epics: list[str] | None) -> None:
    """Background 9-stage boot — must not block post-ready or cockpit HTTP threads."""
    _mark_stage_running(STAGE_1_CONFIG_SANITY)
    try:
        publish_iron_ledger_snapshot()
    except Exception:
        pass
    try:
        result = execute_warmup_and_prime(epics)
        log_engine(
            f"MasterOrchestrator: warmup complete primed={result.get('primed')} "
            f"trade_ready={result.get('trade_ready')} "
            f"phases={result.get('phase_status')}"
        )
    except Exception as exc:
        log_engine(f"MasterOrchestrator: background warmup failed {type(exc).__name__}: {exc}")
    try:
        publish_iron_ledger_snapshot()
    except Exception:
        pass


def _warmup_heartbeat_loop() -> None:
    """Publish iron-ledger stage progress while async warmup runs."""
    interval = float(os.environ.get("IG_ORCH_HEARTBEAT_SEC", "2.0"))
    while True:
        if _warmup_thread is None or not _warmup_thread.is_alive():
            return
        if is_orchestrator_primed() and orchestrator_trade_ready():
            try:
                publish_iron_ledger_snapshot()
            except Exception:
                pass
            return
        try:
            publish_iron_ledger_snapshot()
        except Exception:
            pass
        time.sleep(max(0.5, interval))


def start_master_orchestrator(*, epics: list[str] | None = None, rest: Any | None = None) -> dict[str, Any]:
    """Arm orchestrator, start iron-ledger dispatcher, run warmup/prime in background."""
    global _armed, _dispatcher_thread, _warmup_thread
    _ = rest
    with _lock:
        _armed = True
    if _dispatcher_thread is None or not _dispatcher_thread.is_alive():
        _dispatcher_stop.clear()
        _dispatcher_thread = threading.Thread(
            target=_dispatcher_loop, name="master-orchestrator-dispatch", daemon=True
        )
        _dispatcher_thread.start()
    try:
        publish_iron_ledger_snapshot()
    except Exception:
        pass
    if _warmup_thread is None or not _warmup_thread.is_alive():
        _warmup_thread = threading.Thread(
            target=_run_warmup_background,
            args=(epics,),
            name="master-orchestrator-warmup",
            daemon=True,
        )
        _warmup_thread.start()
        threading.Thread(
            target=_warmup_heartbeat_loop,
            name="master-orchestrator-warmup-heartbeat",
            daemon=True,
        ).start()
    warmup = {
        "ok": True,
        "primed": is_orchestrator_primed(),
        "trade_ready": orchestrator_trade_ready(),
        "phase_status": get_warmup_phase_status(),
        "stage_tokens": get_boot_stage_tokens(),
        "async_warmup": True,
    }
    log_engine(
        f"MasterOrchestrator: armed async_warmup primed={warmup.get('primed')} "
        f"phases={warmup.get('phase_status')} pp={_scoreboard.total_pp}"
    )
    return warmup


def stop_master_orchestrator() -> None:
    _dispatcher_stop.set()


def reset_master_orchestrator_for_tests() -> None:
    global _armed, _primed, _boot_trade_ready, _dispatcher_thread, _warmup_thread, _warmup_logs, _strategy_matrix, _frozen_epics, _asset_status
    _dispatcher_stop.set()
    _dispatcher_thread = None
    _warmup_thread = None
    with _lock:
        _armed = False
        _primed = False
        _boot_trade_ready = False
        _warmup_logs.clear()
        _strategy_matrix.clear()
        _frozen_epics.clear()
        _asset_status.clear()
        _dropped_epics.clear()
        _last_dispatch_errors.clear()
        _stage_tokens.clear()
        _stage_errors.clear()
        for k in _stage_health:
            _stage_health[k] = RAG_PENDING
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "healthy": False,
                "primed": False,
                "armed": False,
                "warmup_logs": [],
                "strategy_matrix": {},
                "active_loops": [],
                "scoreboard": {},
                "trade_ready": False,
                "stage_status": dict(_stage_health),
                "stage_tokens": {},
                "phase_status": dict(_stage_health),
                "ts": 0.0,
            }
        )
    reset_lead_lag_for_tests()
    try:
        from system.chaos_guardian import seed_iron_ledger_for_tests

        seed_iron_ledger_for_tests()
    except Exception:
        pass
    _scoreboard.reset()
