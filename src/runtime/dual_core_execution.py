"""
Dual-core execution plane — Macro Breakout Sentinel + Micro-Slippage Scalper.

Volatility compression (Z < 2.44 demo ceiling) arms ENGINE_B_MICRO_SCALPER mean-reversion
harvesting; Z < 0.00 marks high-conviction compressed setups; expansion (Z >= 2.45)
favours MACRO_BREAKOUT_SENTINEL.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque

import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

_execution_logger = logging.getLogger("ig_agent.parallel_strategy_sweep")
PIERCE_LOWER_Z = -2.00
PIERCE_UPPER_Z = 2.00
# Aligned with the platform-wide 45s hub quote-trust window. The sweep now
# sees real quote ages (fetch wall-clock previously masked staleness); Yahoo
# reference quotes for indices routinely age 10-45s between poll cycles, so a
# tighter gate caused constant rotation churn and suppressed trading.
STAGNANT_QUOTE_SEC = 45.0
_sweep_dispatch_lock = threading.Lock()

MACRO_Z_THRESHOLD = 2.45
MICRO_Z_THRESHOLD = 2.44  # demo: arm Core B for all non-macro Z (incl. neutral dead zone)
MICRO_HIGH_CONVICTION_Z = 0.00  # Z below this = highly valid compressed setup
# Temporary manual override — any rolling Z in this band arms Core B (clears Gate 5 dead zone).
CORE_B_FORCE_CHANNEL_Z_MIN = -2.00
CORE_B_FORCE_CHANNEL_Z_MAX = 2.00
# Lower piercing zone — any Z at or below -2.00 is a valid Core B setup (incl. deep compression).
CORE_B_PIERCE_LOWER_Z = -2.00
CORE_B_PIERCE_UPPER_Z = 2.00
CORE_B_FORCE_CHANNEL_OVERRIDE = True
FOREX_SESSION_LOCK_REASON = "forex_rotation_locked"
DEMO_BYPASS_15M_MACRO_TREND_LOCK = True
CORE_B_SATELLITE_UNCOUPLED = True  # test profile: Core B ignores 15m macro directional lock
CANARY_FX_LOT = 1.0
CANARY_INDEX_LOT = 1.0
CANARY_GOLD_LOT = 1.0
CANARY_GOLD_LOT_SPREADBET = 10.0
CANARY_INDEX_LOT_SPREADBET = 0.5
PRIMARY_STACKED_EPIC = "IX.D.DOW.IFM.IP"
SECONDARY_STACKED_EPIC = "CS.D.CFPGOLD.CFP.IP"
DEFAULT_TRADEABLE_STACK: tuple[str, ...] = (PRIMARY_STACKED_EPIC, "IX.D.NIKKEI.IFM.IP")
STACKED_DUAL_ASSETS: tuple[str, ...] = DEFAULT_TRADEABLE_STACK
FAILOVER_TOP_EPIC = "CS.D.EURUSD.CFD.IP"
FAILOVER_BOTTOM_EPIC = "CS.D.GBPUSD.CFD.IP"
FAILOVER_FOREX_STACK: tuple[str, ...] = (FAILOVER_TOP_EPIC, FAILOVER_BOTTOM_EPIC)
FAILOVER_STATE_NORMAL = "NORMAL"
FAILOVER_STATE_FOREX_LOCKED = "FOREX_FAILOVER"
MIN_TICKS_PER_MINUTE = 5
EVICT_DEADLINE_SEC = 0.5
TICK_VELOCITY_WINDOW_SEC = 60.0
_DEFAULT_MAX_SPREAD_PTS: dict[str, float] = {
    PRIMARY_STACKED_EPIC: 12.0,
    SECONDARY_STACKED_EPIC: 8.0,
    FAILOVER_TOP_EPIC: 3.0,
    FAILOVER_BOTTOM_EPIC: 4.0,
    "CS.D.CRUDE.CFD.IP": 10.0,
    "IX.D.NIKKEI.IFM.IP": 15.0,
    "IX.D.FTSE.IFM.IP": 12.0,
    "IX.D.DAX.IFM.IP": 12.0,
}
STACKED_POLL_SEC = 0.5  # 500ms multi-source rotation sweep
ROTATION_SWEEP_SEC = STACKED_POLL_SEC
ROTATION_UNIVERSE: tuple[str, ...] = NIGHT_MATRIX_EPICS
ACTIVE_STACK_SLOTS = 2
STAGNANT_Z_MIN = -0.50
STAGNANT_Z_MAX = +0.50
STAGNANT_DEAD_ZONE_SEC = 300.0
STAGNANT_DEAD_ZONE_REASON = "STAGNANT_DEAD_ZONE"
MULTI_SOURCE_AUTO_ROTATION = True
MICRO_SCALP_INSTANT_MIN_SCORE = 45.0
MICRO_SCALP_TARGET_MIN_PTS = 1.5
MICRO_SCALP_TARGET_MAX_PTS = 4.0
# Event-driven default: no multi-second cadence smoothing on the tick lane.
MICRO_SCALP_INSTANT_CADENCE_SEC = 0.0
_micro_scalper_lane_registered = False
_micro_scalper_lane_unsub: Callable[[], None] | None = None
_last_instant_scalp_at: dict[str, float] = {}
_micro_scalper_lane_lock = threading.Lock()


def multi_source_auto_rotation_enabled(
    cfg: Any | None = None,
    *,
    _lock_held: bool = False,
) -> bool:
    """False when config or runtime forex lock pins EUR/USD + GBP/USD hot path."""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                if dual.get("forex_rotation_locked"):
                    return False
                if "multi_source_auto_rotation" in dual:
                    return bool(dual.get("multi_source_auto_rotation"))
    except Exception:
        pass
    if _lock_held:
        if _forex_rotation_locked:
            return False
    else:
        with _lock:
            if _forex_rotation_locked:
                return False
    return MULTI_SOURCE_AUTO_ROTATION


def _epics_with_open_positions() -> set[str]:
    """Epics with live lifecycle trades — pinned on stack during rotation."""
    pinned: set[str] = set()
    try:
        from runtime.trade_lifecycle import snapshot as lifecycle_snapshot

        for trade in (lifecycle_snapshot().get("active") or {}).values():
            if not isinstance(trade, dict):
                continue
            epic = str(trade.get("epic") or "").strip()
            if epic:
                pinned.add(epic)
    except Exception:
        pass
    return pinned


def _resolve_stack_rest_client(cfg: Any | None = None) -> Any | None:
    try:
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import get_shared_rest_client

        cred = try_load_credentials()
        if cred.ok and cred.credentials:
            return get_shared_rest_client(cred.credentials)
    except Exception:
        pass
    return None


def resolve_tradeable_stack_epics(
    rest: Any | None,
    cfg: Any | None = None,
    *,
    universe: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Filter rotation universe to epics where IG allows new deals."""
    pool = universe or ROTATION_UNIVERSE
    if rest is None:
        return DEFAULT_TRADEABLE_STACK
    try:
        from execution.broker_tradeability import broker_new_deal_allowed
    except Exception:
        return DEFAULT_TRADEABLE_STACK
    tradeable: list[str] = []
    excluded: set[str] = set()
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                excluded = {str(e).strip() for e in (dual.get("exclude_from_hot_path") or [])}
    except Exception:
        excluded = set()
    for epic in pool:
        if epic in excluded:
            continue
        ok, _ = broker_new_deal_allowed(rest, epic, cfg=cfg)
        if ok:
            tradeable.append(epic)
    return tuple(tradeable) if tradeable else DEFAULT_TRADEABLE_STACK


_tradeability_refresh_inflight = threading.Event()


def refresh_active_stack_tradeability_async(
    *, cfg: Any | None = None, rest: Any | None = None
) -> bool:
    """Fire-and-forget tradeability refresh — never blocks the 500ms sweep.

    Cache-miss probes inside refresh can wait up to 30s on the IG rate-limit
    token bucket; running them inline froze whole sweep iterations whenever
    the ig_ledger bucket was dry.
    """
    if _tradeability_refresh_inflight.is_set():
        return False
    now = time.time()
    if now - _last_tradeability_refresh_at < _TRADEABILITY_REFRESH_MIN_SEC:
        return False
    _tradeability_refresh_inflight.set()

    def _run() -> None:
        try:
            refresh_active_stack_tradeability(cfg=cfg, rest=rest)
        except Exception as exc:
            log_engine(
                f"TradeabilityRefresh: async guard {type(exc).__name__}: {exc}"
            )
        finally:
            _tradeability_refresh_inflight.clear()

    threading.Thread(
        target=_run,
        name="stack-tradeability-refresh",
        daemon=True,
    ).start()
    return True


def refresh_active_stack_tradeability(*, cfg: Any | None = None, rest: Any | None = None) -> bool:
    """Drop non-tradeable epics from active stack; backfill from tradeable universe."""
    global _last_tradeability_refresh_at
    now = time.time()
    if now - _last_tradeability_refresh_at < _TRADEABILITY_REFRESH_MIN_SEC:
        return False
    _last_tradeability_refresh_at = now
    if rest is None:
        rest = _resolve_stack_rest_client(cfg)
    tradeable = resolve_tradeable_stack_epics(rest, cfg)
    tradeable_set = set(tradeable)
    slots = get_active_stack_slots(cfg)
    current = get_active_stack_epics()
    kept = tuple(epic for epic in current if epic in tradeable_set)
    if len(kept) >= slots and len(kept) == len(current):
        return False
    picks = list(kept)
    for epic in tradeable:
        if epic not in picks:
            picks.append(epic)
        if len(picks) >= slots:
            break
    if len(picks) < slots:
        for epic in DEFAULT_TRADEABLE_STACK:
            if epic not in picks:
                picks.append(epic)
            if len(picks) >= slots:
                break
    new_stack = tuple(picks[:slots])
    if not new_stack or new_stack == current:
        return False
    return _rotate_active_stack_to(new_stack, reason="tradeability_refresh", cfg=cfg, rest=rest)


def epic_allowed_on_hot_path(epic: str, cfg: Any | None = None) -> bool:
    """Reject dispatch on epics outside active stack or on exclude list.

    Config ``exclude_from_hot_path`` is authoritative. The desk hot-path epic
    (DOW) must never be false-blocked by in-memory stack races or chop freeze —
    those gates belong downstream (regime veto / strategy matrix), not here.
    Misleading ``hot_path_epic_excluded`` logs for DOW starve fills while
    ``trading_path_live`` stays green.

    Forex-rotation lock remains a hard override (indices off).
    """
    key = str(epic or "").strip()
    if not key:
        return False

    excluded: set[str] = set()
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                excluded = {
                    str(e).strip() for e in (dual.get("exclude_from_hot_path") or [])
                }
                if dual.get("forex_rotation_locked"):
                    # Config-level forex lock — only EUR/GBP stack (handled below
                    # via active stack / exploration); DOW is not authoritative.
                    if key == "IX.D.DOW.IFM.IP":
                        return False
    except Exception:
        excluded = set()
    if key in excluded:
        return False

    # multi_market_promote: prepared gates stay OFF until enabled; Nikkei never
    # auto-promotes (nikkei_hot_path must be explicit true + removed from exclude).
    try:
        if cfg is not None and hasattr(cfg, "get"):
            promo = cfg.get("multi_market_promote") or {}
            if isinstance(promo, dict):
                if key == "IX.D.NIKKEI.IFM.IP" and not bool(promo.get("nikkei_hot_path")):
                    return False
                prepared = promo.get("prepared_gates") or {}
                if isinstance(prepared, dict) and key in prepared:
                    gate = prepared.get(key) or {}
                    if not bool(gate.get("enabled")):
                        return False
                hot = promo.get("hot_path_epics")
                if isinstance(hot, list) and hot and key not in {
                    str(e).strip() for e in hot
                }:
                    # Non-listed epics still need exploration / route allow below
                    # unless they are the authoritative DOW hot path.
                    if key != "IX.D.DOW.IFM.IP":
                        pass
    except Exception:
        pass

    try:
        if is_forex_failover_active() and key == "IX.D.DOW.IFM.IP":
            return False
    except Exception:
        pass

    # Authoritative demo/live hot path — DOW stays dispatchable when not excluded
    # and forex failover is off.
    if key == "IX.D.DOW.IFM.IP":
        return True

    try:
        if cfg is not None and hasattr(cfg, "get"):
            ehp = cfg.get("engine_hot_path") or {}
            if isinstance(ehp, dict) and bool(ehp.get("enabled")):
                armed = {str(e).strip() for e in (ehp.get("epics") or [])}
                if key in armed and key in get_active_stack_epics():
                    return True
    except Exception:
        pass

    try:
        from runtime.portfolio_exploration_engine import exploration_allows_hot_path

        if exploration_allows_hot_path(key, cfg):
            return True
    except Exception:
        pass
    try:
        from runtime.master_orchestrator import get_strategy_route, route_allows_entry

        route = get_strategy_route(key)
        if route is not None:
            path = str(route.get("execution_path") or "")
            if path == "limit_chase_hf":
                return True
            if path == "momentum_breakout":
                return True
        allowed, reason = route_allows_entry(key)
        if not allowed and reason.startswith("master_orchestrator_chop"):
            return False
    except Exception:
        pass
    if key not in get_active_stack_epics():
        return False
    return True


MODE_MACRO = "MACRO_BREAKOUT_SENTINEL"
MODE_MICRO = "LIGHTNING_MICRO_SCALPER"
MODE_NEUTRAL = "NEUTRAL"
ENGINE_B_MICRO_SCALPER = "ENGINE_B_MICRO_SCALPER"

MICRO_TP_POINTS = 1.5
MICRO_SL_POINTS = 2.0
Z_ROLLING_WINDOW = 20  # 20-tick rolling volatility window (telemetry export)
# Legacy alias — stacked dual-asset mode replaces erratic cascade switching.
COGNITIVE_CASCADE_EPICS = STACKED_DUAL_ASSETS
CASCADE_SWEEP_SEC = STACKED_POLL_SEC
_SHORT_WINDOW = 30
_LONG_WINDOW = 120
_MIN_SAMPLES = 12
_Z_HISTORY_MAX = 120


@dataclass
class DualCoreSnapshot:
    volatility_z_score: float
    execution_mode: str
    core_a_macro_active: bool
    core_b_micro_active: bool
    engine_b_armed: bool
    micro_channel_upper: float | None
    micro_channel_lower: float | None
    epic: str
    live_calculated_zscore: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "volatility_z_score": round(self.volatility_z_score, 4),
            "live_calculated_zscore": round(self.live_calculated_zscore, 4),
            "execution_mode": self.execution_mode,
            "core_a_macro_active": self.core_a_macro_active,
            "core_b_micro_active": self.core_b_micro_active,
            "engine_b_armed": self.engine_b_armed,
            "micro_channel_upper": self.micro_channel_upper,
            "micro_channel_lower": self.micro_channel_lower,
            "epic": self.epic,
            "updated_at": self.updated_at,
        }


_lock = threading.Lock()
_ROTATION_LOCK_TIMEOUT_SEC = 0.25
_snapshot = DualCoreSnapshot(
    volatility_z_score=0.0,
    live_calculated_zscore=0.0,
    execution_mode=MODE_NEUTRAL,
    core_a_macro_active=False,
    core_b_micro_active=False,
    engine_b_armed=False,
    micro_channel_upper=None,
    micro_channel_lower=None,
    epic="",
)
_mid_history: dict[str, deque[float]] = {
    epic: deque(maxlen=_LONG_WINDOW) for epic in NIGHT_MATRIX_EPICS
}
_width_history: dict[str, deque[float]] = {
    epic: deque(maxlen=_LONG_WINDOW) for epic in NIGHT_MATRIX_EPICS
}
_z_history: deque[float] = deque(maxlen=_Z_HISTORY_MAX)
_z_history_by_epic: dict[str, deque[float]] = {
    epic: deque(maxlen=_Z_HISTORY_MAX) for epic in ROTATION_UNIVERSE
}
_snapshots: dict[str, DualCoreSnapshot] = {}
_last_gate_suppression_reason: str = ""
_execution_focus_target: str = PRIMARY_STACKED_EPIC
_focus_tick_velocity: float = 0.0
_velocity_by_epic: dict[str, float] = {}
_last_mid_by_epic: dict[str, float] = {}
_tick_arrivals: dict[str, deque[float]] = {
    epic: deque(maxlen=256) for epic in ROTATION_UNIVERSE
}
_ml_dynamic_overrides: dict[str, Any] = {}
_ml_sovereignty_active: bool = False
_failover_state: str = FAILOVER_STATE_NORMAL
_failover_active: bool = False
_failover_reason: str = ""
_forex_rotation_locked: bool = False
_evicted_epics: set[str] = set()
_evict_deadline_by_epic: dict[str, float] = {}
_active_stack_epics: tuple[str, ...] = STACKED_DUAL_ASSETS
_stagnant_since_by_epic: dict[str, float] = {}
_last_rotation_at: float = 0.0
_last_rotation_reason: str = ""
_rotation_sweep_count: int = 0
_last_tradeability_refresh_at: float = 0.0
_TRADEABILITY_REFRESH_MIN_SEC = 30.0
_rotation_bootstrap_complete: bool = False
_stacked_stop = threading.Event()
_stacked_thread: threading.Thread | None = None
_stacked_tracks_started_at: float = 0.0

# Escape hatch — triggered when all active stack epics have tpm=0 for >60s
ESCAPE_HATCH_TPM_ZERO_SEC = 60.0
# P1 — rehydrate feeds (no rotation) when stack tpm=0 for >30s
TPM_ZERO_REHYDRATE_SEC = 30.0
BOOT_GRACE_SEC = 180.0
_escape_all_tpm_zero_since: float = 0.0
_tpm_zero_rehydrate_since: float = 0.0
_rotation_escape_active: bool = False
_rotation_history: deque[dict[str, Any]] = deque(maxlen=25)

# Socket heartbeat — stale stream detection + non-blocking rehydration
SOCKET_STALE_SEC = 5.0
# Minimum spacing between full stream rehydrations. Without this the heartbeat
# re-armed Yahoo + IG pollers every ~8s while quotes stayed stale (e.g. REST
# radio silence), leaking a poll thread per pass until the process seized up.
REHYDRATE_COOLDOWN_SEC = 60.0
_last_fresh_tick_at: dict[str, float] = {}
_socket_channel_state: dict[str, str] = {}
_stream_reset_inflight = False
_last_rehydrate_started_at = 0.0
_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None


def _record_quote_pulse(epic: str) -> None:
    """Record a successful quote fetch into tick arrivals (unified tick counter)."""
    key = str(epic or "").strip()
    if not key:
        return
    now = time.time()
    arrivals = _tick_arrivals.get(key)
    if arrivals is None:
        with _lock:
            arrivals = _tick_arrivals.setdefault(key, deque(maxlen=256))
    arrivals.append(now)


def _ingest_fresh_quote(
    epic: str,
    bid: float,
    offer: float,
    *,
    cfg: Any | None = None,
    source: str = "yahoo",
    quote_time: float | None = None,
) -> DualCoreSnapshot | None:
    """Unified ingest — mid → Z history + tick pulse + fresh tick marker.

    ``quote_time`` must be forwarded when re-ingesting a hub snapshot so the
    republish keeps the original quote epoch instead of masking a stale quote
    as freshly ticked.
    """
    key = str(epic or "").strip()
    if not key or bid <= 0 or offer <= 0:
        return None
    published = False
    try:
        get_market_data_hub().publish(
            key,
            float(bid),
            float(offer),
            source=str(source or "yahoo"),
            quote_time=quote_time,
        )
        published = True
    except Exception:
        published = False
    mid = (float(bid) + float(offer)) / 2.0
    snap = ingest_hub_mid(key, mid, cfg=cfg)
    # Never pulse "fresh" on a failed publish or a republished stale epoch —
    # that was masking 200s+ hub ages as LIVE TPM / health_light trade_ready.
    if published:
        try:
            from system.market_integrity import effective_entry_quote_budget_sec

            budget = float(effective_entry_quote_budget_sec(cfg))
        except Exception:
            budget = 10.0
        age = 0.0
        if quote_time is not None:
            try:
                age = max(0.0, time.time() - float(quote_time))
            except (TypeError, ValueError):
                age = 0.0
        if age <= budget:
            _mark_fresh_tick(key)
            _record_quote_pulse(key)
    return snap


def _mark_fresh_tick(epic: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    now = time.time()
    with _lock:
        _last_fresh_tick_at[key] = now
        _socket_channel_state[key] = "STREAMING"


def get_socket_heartbeat_state() -> dict[str, Any]:
    """Dashboard / telemetry — per-epic socket channel health."""
    with _lock:
        return {
            "socket_stale_sec": SOCKET_STALE_SEC,
            "channel_state": dict(_socket_channel_state),
            "last_fresh_tick_at": {k: round(v, 3) for k, v in _last_fresh_tick_at.items()},
            "reset_inflight": bool(_stream_reset_inflight),
        }


_IG_REARM_ATTEMPTS = 3
_IG_REARM_BACKOFF_SEC = (1.0, 3.0, 9.0)


def _rearm_ig_stream_with_retry(cfg_obj: Any | None) -> bool:
    """Re-arm the IG market stream with bounded exponential backoff."""
    if cfg_obj is None:
        return False
    for attempt in range(1, _IG_REARM_ATTEMPTS + 1):
        try:
            from runtime.agent_bootstrap import start_market_stream
            from system.credentials_loader import try_load_credentials
            from system.ig_rest_session import get_shared_rest_client

            cred = try_load_credentials()
            if not cred.ok or cred.credentials is None:
                log_engine("SocketHeartbeat: IG stream re-arm skipped — no credentials")
                return False
            start_market_stream(
                cfg_obj,
                rest_client=get_shared_rest_client(cred.credentials),
                clear_stream_ready=False,
            )
            if attempt > 1:
                log_engine(f"SocketHeartbeat: IG stream re-armed on attempt {attempt}")
            return True
        except Exception as exc:
            log_engine(
                f"SocketHeartbeat: IG stream re-arm attempt {attempt}/{_IG_REARM_ATTEMPTS} "
                f"failed {type(exc).__name__}: {exc}"
            )
            if attempt < _IG_REARM_ATTEMPTS:
                time.sleep(_IG_REARM_BACKOFF_SEC[attempt - 1])
    return False


def _trigger_non_blocking_stream_rehydration(stale_epics: list[str]) -> None:
    """Reset Yahoo/IG feed without blocking the scoring loop thread."""
    global _stream_reset_inflight, _last_rehydrate_started_at
    if not stale_epics:
        return
    with _lock:
        if _stream_reset_inflight:
            return
        now = time.time()
        if (now - _last_rehydrate_started_at) < REHYDRATE_COOLDOWN_SEC:
            return
        _last_rehydrate_started_at = now
        _stream_reset_inflight = True

    def _worker() -> None:
        global _stream_reset_inflight
        try:
            log_engine(
                f"SocketHeartbeat: SOCKET_STALE on {stale_epics} — non-blocking rehydrate"
            )
            from feeder.yahoo_quote_poller import (
                start_yahoo_quote_poller,
                stop_yahoo_quote_poller,
                yahoo_poller_active,
            )
            from feeder.pricing_transport import yahoo_poll_seconds
            from system.stream_ready import reset_stream_ready

            reset_stream_ready()
            # Only bounce the Yahoo poller when it is actually dead. A healthy
            # poller going through stop/start every rehydrate cycle left
            # multi-second polling gaps that aged the whole hub past the
            # stagnation gate and cascaded into rotation churn.
            poller_healthy = False
            try:
                poller_healthy = bool(yahoo_poller_active())
            except Exception:
                poller_healthy = False
            if not poller_healthy:
                stop_yahoo_quote_poller()

                async def _pause_yield() -> None:
                    await asyncio.sleep(0.25)

                asyncio.run(_pause_yield())
            epics = list(
                dict.fromkeys(
                    list(get_active_stack_epics()) + list(NIGHT_MATRIX_EPICS)
                )
            )
            # start_market_stream requires a Config object (calls .as_dict());
            # passing ConfigLoader().load()'s plain dict here broke every IG
            # stream re-arm with AttributeError since the load() migration.
            cfg_obj = None
            cfg_dict = None
            try:
                from system.config_loader import ConfigLoader

                cfg_obj = ConfigLoader().load_config(validate=False)
                cfg_dict = cfg_obj.as_dict()
            except Exception as exc:
                log_engine(
                    f"SocketHeartbeat: config reload failed {type(exc).__name__}: {exc}"
                )
            # On a live poller this only refreshes the epic set; a dead poller
            # is restarted from scratch.
            start_yahoo_quote_poller(epics, poll_sec=yahoo_poll_seconds(cfg_dict))
            _rearm_ig_stream_with_retry(cfg_obj)
            # Do NOT _mark_fresh_tick here — rehydrate only restarts pollers;
            # freshness must come from a successful hub.publish with a live epoch.
        except Exception as exc:
            log_engine(f"SocketHeartbeat: rehydrate failed {type(exc).__name__}: {exc}")
        finally:
            with _lock:
                _stream_reset_inflight = False

    threading.Thread(
        target=_worker,
        name="socket-heartbeat-rehydrate",
        daemon=True,
    ).start()


def validate_socket_heartbeat() -> dict[str, Any]:
    """
    Heartbeat validator — flags SOCKET_STALE when no fresh tick for 5s on active stack.
    """
    hub = get_market_data_hub()
    now = time.time()
    stale_epics: list[str] = []
    stack = get_active_stack_epics()
    for epic in stack:
        quote = hub.get_snapshot(epic)
        fresh = (
            quote is not None
            and float(getattr(quote, "bid", 0) or 0) > 0
            and float(getattr(quote, "offer", 0) or 0) > 0
            and float(quote.age_seconds()) <= 45.0
        )
        if fresh:
            _ingest_fresh_quote(
                epic,
                float(quote.bid),
                float(quote.offer),
                source=str(getattr(quote, "source", None) or "hub"),
            )
            continue
        last = float(_last_fresh_tick_at.get(epic) or 0.0)
        if last <= 0.0 or (now - last) > SOCKET_STALE_SEC:
            stale_epics.append(epic)
            with _lock:
                _socket_channel_state[epic] = "SOCKET_STALE"
    if stale_epics:
        _trigger_non_blocking_stream_rehydration(stale_epics)
    _check_tpm_zero_rehydrate(cfg=None)
    return {"stale_epics": stale_epics, **get_socket_heartbeat_state()}


async def socket_heartbeat_validator_loop(interval_sec: float = 1.0) -> None:
    """Async wrapper — runs validate_socket_heartbeat off the event loop."""
    import asyncio

    while True:
        try:
            await asyncio.to_thread(validate_socket_heartbeat)
        except Exception as exc:
            log_engine(f"SocketHeartbeat: async validator {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(0.5, float(interval_sec)))


def start_socket_heartbeat_validator(*, interval_sec: float = 1.0) -> None:
    """Daemon thread heartbeat — pairs with DualCoreCoordinator poll loop."""
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()

    def _sync_loop() -> None:
        while not _heartbeat_stop.wait(max(0.5, float(interval_sec))):
            try:
                validate_socket_heartbeat()
            except Exception as exc:
                log_engine(f"SocketHeartbeat: validator {type(exc).__name__}: {exc}")

    _heartbeat_thread = threading.Thread(
        target=_sync_loop,
        name="socket-heartbeat-validator",
        daemon=True,
    )
    _heartbeat_thread.start()
    log_engine(
        f"SocketHeartbeat: async validator armed (stale>{SOCKET_STALE_SEC}s → rehydrate)"
    )


def epic_display_name(epic: str) -> str:
    e = str(epic or "").upper()
    if "DOW" in e:
        return "Wall Street"
    if "CFPGOLD" in e or "GOLD" in e:
        return "Gold"
    if "EURUSD" in e:
        return "EUR/USD"
    if "GBPUSD" in e:
        return "GBP/USD"
    return epic or "UNKNOWN"


def get_execution_focus_target() -> str:
    with _lock:
        if _failover_active:
            return FAILOVER_TOP_EPIC
        return str(_execution_focus_target or "")


def get_active_stack_epics() -> tuple[str, ...]:
    if not _lock.acquire(timeout=_ROTATION_LOCK_TIMEOUT_SEC):
        return STACKED_DUAL_ASSETS
    try:
        return tuple(_active_stack_epics)
    finally:
        _lock.release()


def is_forex_failover_active() -> bool:
    with _lock:
        return bool(_failover_active or _forex_rotation_locked)


def is_forex_rotation_locked() -> bool:
    with _lock:
        return bool(_forex_rotation_locked)


def lock_forex_rotation_session(
    *,
    reason: str = FOREX_SESSION_LOCK_REASON,
    cfg: Any | None = None,
    rest: Any | None = None,
) -> dict[str, Any]:
    """Force 100% execution focus onto EUR/USD + GBP/USD hot path (no index/metal)."""
    global _failover_state, _failover_active, _failover_reason, _active_stack_epics
    global _execution_focus_target, _ml_sovereignty_active, _forex_rotation_locked
    try:
        from system.config_loader import load_active_config
        from execution.broker_epic_resolver import (
            resolve_account_product,
            resolve_hot_path_epics_from_config,
        )
        from execution.broker_tradeability import broker_new_deal_allowed

        if cfg is None:
            cfg = load_active_config(validate=False)
        if rest is None:
            rest = _resolve_stack_rest_client(cfg)
        forex_stack = resolve_hot_path_epics_from_config(cfg, rest=rest)
        if rest is not None:
            for fx_epic in forex_stack:
                ok_trade, block_reason = broker_new_deal_allowed(rest, fx_epic, cfg=cfg)
                if not ok_trade:
                    log_engine(
                        f"ForexRotationLock: blocked epic={fx_epic} "
                        f"reason={block_reason or 'not_tradeable'}"
                    )
                    return get_failover_state()
        top_epic = forex_stack[0] if forex_stack else FAILOVER_TOP_EPIC
        product = resolve_account_product(cfg=cfg, rest=rest)
        log_engine(
            f"ForexRotationLock: account_product={product} logical_stack={list(forex_stack)}"
        )
    except Exception:
        forex_stack = FAILOVER_FOREX_STACK
        top_epic = FAILOVER_TOP_EPIC
    with _lock:
        _forex_rotation_locked = True
        _failover_state = FAILOVER_STATE_FOREX_LOCKED
        _failover_active = True
        _failover_reason = str(reason or FOREX_SESSION_LOCK_REASON)
        _active_stack_epics = forex_stack
        _execution_focus_target = top_epic
        _ml_sovereignty_active = True
        _evicted_epics.clear()
    log_engine(
        f"ForexRotationLock: session locked stack={list(forex_stack)} reason={reason}"
    )
    return get_failover_state()


def get_failover_state() -> dict[str, Any]:
    with _lock:
        fx_locked = bool(_failover_active or _forex_rotation_locked)
        return {
            "failover_active": fx_locked,
            "forex_rotation_locked": bool(_forex_rotation_locked),
            "failover_state": str(_failover_state),
            "failover_reason": str(_failover_reason),
            "failover_targets": list(_active_stack_epics) if fx_locked else [],
            "failover_top_epic": _active_stack_epics[0] if fx_locked and _active_stack_epics else "",
            "failover_bottom_epic": (
                _active_stack_epics[1]
                if fx_locked and len(_active_stack_epics) > 1
                else ""
            ),
            "evicted_epics": sorted(_evicted_epics),
            "active_stack_epics": list(_active_stack_epics),
            **get_rotation_state_locked(),
        }


def get_rotation_state() -> dict[str, Any]:
    if not _lock.acquire(timeout=_ROTATION_LOCK_TIMEOUT_SEC):
        return {
            "snapshot_stale": True,
            "multi_source_auto_rotation": MULTI_SOURCE_AUTO_ROTATION,
            "rotation_universe": list(ROTATION_UNIVERSE),
            "rotation_sweep_sec": ROTATION_SWEEP_SEC,
            "rotation_sweep_count": 0,
            "active_stack_epics": list(STACKED_DUAL_ASSETS),
        }
    try:
        return get_rotation_state_locked()
    finally:
        _lock.release()


def get_active_stack_slots(cfg: Any | None = None) -> int:
    """Configurable parallel stack depth (default 2; demo throughput may use 3)."""
    if cfg is not None:
        try:
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict) and dual.get("active_stack_slots") is not None:
                return max(2, min(4, int(dual["active_stack_slots"])))
        except (TypeError, ValueError):
            pass
        try:
            block = cfg.get("demo_throughput_mode") or {}
            if isinstance(block, dict) and block.get("enabled"):
                return max(2, int((cfg.get("dual_core") or {}).get("active_stack_slots", 3)))
        except (TypeError, ValueError):
            pass
    return ACTIVE_STACK_SLOTS


def get_rotation_eligibility(cfg: Any | None = None) -> dict[str, Any]:
    """Per-instrument eligibility for multi-market rotation panel."""
    with _lock:
        return _rotation_eligibility_unlocked(cfg)


def _rotation_eligibility_unlocked(cfg: Any | None = None) -> dict[str, Any]:
    stack = set(get_active_stack_epics())
    ranked = _rank_universe_by_velocity(cfg=cfg)
    velocity_map = {epic: vel for epic, vel in ranked}
    active: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    for epic in ROTATION_UNIVERSE:
        tpm = _ticks_per_minute(epic)
        z = 0.0
        try:
            snap = _snapshots.get(epic)
            if snap is not None:
                z = float(snap.volatility_z_score)
        except Exception:
            pass
        spread_ok = True
        try:
            from system.market_data_hub import get_market_data_hub

            q = get_market_data_hub().get_snapshot(epic)
            if q is not None and q.bid > 0 and q.offer > 0:
                spread_ok, _ = _channel_health_ok(epic, q.bid, q.offer, cfg=cfg)
        except Exception:
            pass
        row = {
            "epic": epic,
            "label": epic_display_name(epic),
            "ticks_per_minute": tpm,
            "velocity": round(float(velocity_map.get(epic, 0.0)), 4),
            "z_score": round(z, 4),
            "spread_ok": spread_ok,
        }
        if epic in stack:
            row["reason"] = "active_stack"
            active.append(row)
        elif tpm >= MIN_TICKS_PER_MINUTE and spread_ok:
            row["reason"] = "eligible_velocity"
            eligible.append(row)
        else:
            reasons = []
            if tpm < MIN_TICKS_PER_MINUTE:
                reasons.append("low_velocity")
            if not spread_ok:
                reasons.append("spread_wide")
            row["reason"] = ",".join(reasons) or "quiet"
            inactive.append(row)
    return {
        "active_instruments": active,
        "eligible_instruments": eligible,
        "inactive_instruments": inactive,
        "active_stack_slots": get_active_stack_slots(cfg),
    }


def get_rotation_state_locked() -> dict[str, Any]:
    """Caller must hold ``_lock``."""
    stagnant: dict[str, float] = {}
    now = time.time()
    for epic, since in _stagnant_since_by_epic.items():
        stagnant[epic] = round(max(0.0, now - since), 1)
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception:
        cfg = None
    dead_zone_sec = _resolve_stagnant_dead_zone_sec(cfg)
    pinned = sorted(_epics_with_open_positions())
    return {
        "multi_source_auto_rotation": multi_source_auto_rotation_enabled(
            cfg, _lock_held=True
        ),
        "rotation_universe": list(ROTATION_UNIVERSE),
        "rotation_sweep_sec": ROTATION_SWEEP_SEC,
        "last_rotation_at": _last_rotation_at,
        "last_rotation_reason": _last_rotation_reason,
        "rotation_sweep_count": _rotation_sweep_count,
        "stagnant_dead_zone_epics": stagnant,
        "stagnant_z_band": [STAGNANT_Z_MIN, STAGNANT_Z_MAX],
        "stagnant_dead_zone_sec": dead_zone_sec,
        "pinned_open_epics": pinned,
        "rotation_escape_active": bool(_rotation_escape_active),
        **_rotation_eligibility_unlocked(),
        "boot_grace_active": (
            _stacked_tracks_started_at > 0
            and (time.time() - _stacked_tracks_started_at) < BOOT_GRACE_SEC
        ),
        "rotation_scores": _rotation_scores_unlocked(),
        "rotation_history": list(_rotation_history)[-15:],
    }


def _in_quiet_center_channel(z: float) -> bool:
    return STAGNANT_Z_MIN <= float(z) <= STAGNANT_Z_MAX


def _fetch_multi_source_quote(
    epic: str,
    hub: Any,
    *,
    cfg: Any | None = None,
) -> tuple[float, float, str, float] | None:
    """Hub (IG/stream) first, Yahoo Finance fallback — 3-source cross-market scan.

    Hub age gate: if hub age >15s prefer Yahoo first (only reject at >45s).
    Returns (bid, offer, source, quote_epoch) — the epoch is the actual quote
    time so the sweep's stagnation detector sees real freshness instead of the
    fetch wall-clock (which masked stale hub quotes as live data).
    """
    key = str(epic or "").strip()
    if not key:
        return None
    now = time.time()
    quote = hub.get_snapshot(key)
    hub_age = float(quote.age_seconds()) if quote is not None else 999.0
    hub_fresh = (
        quote is not None
        and float(getattr(quote, "bid", 0) or 0) > 0
        and float(getattr(quote, "offer", 0) or 0) > 0
        and hub_age <= 45.0
    )
    hub_preferred = hub_fresh and hub_age <= 15.0

    if hub_preferred:
        return (
            float(quote.bid),
            float(getattr(quote, "offer", 0)),
            str(getattr(quote, "source", None) or "hub"),
            now - hub_age,
        )

    # Hub stale (>15s) or missing — try Yahoo first. token_wait_sec=0: this
    # runs on the 500ms sweep path, so never park the worker waiting for a
    # rate-limit token — fall through to the hub snapshot instantly.
    try:
        from feeder.yahoo_quote_poller import fetch_yahoo_quote

        sample = fetch_yahoo_quote(key, token_wait_sec=0.0)
        if sample is not None and sample.bid > 0 and sample.offer > 0:
            try:
                hub.publish(
                    key,
                    float(sample.bid),
                    float(sample.offer),
                    source="yahoo",
                )
            except Exception:
                pass
            return float(sample.bid), float(sample.offer), "yahoo", time.time()
    except Exception:
        pass

    # Yahoo unavailable — fall back to hub ONLY while still fresh.
    # Never re-ingest hub_stale into the sweep (was pumping TPM / fake LIVE).
    if hub_fresh:
        return (
            float(quote.bid),
            float(getattr(quote, "offer", 0)),
            str(getattr(quote, "source", None) or "hub"),
            now - hub_age,
        )
    return None


def _z_fallback_regime(z: float) -> tuple[str, float]:
    """Display-only fallback when regime_switch snapshot has no row for epic."""
    z_abs = abs(float(z))
    if z_abs >= MACRO_Z_THRESHOLD:
        return "expansion", 55.0
    if z_abs <= 2.0:
        return "compressed", 85.0
    return "neutral", 70.0


def _rotation_score_breakdown(epic: str, *, cfg: Any | None = None) -> dict[str, Any]:
    """Per-epic rotation scoring — volatility, spread, feed, P&L proxy, regime."""
    tpm = float(_ticks_per_minute(epic))
    vel = float(_velocity_by_epic.get(epic, 0.0))
    snap = _snapshots.get(epic)
    z = float(snap.live_calculated_zscore if snap is not None else 0.0)
    spread_ok = True
    spread_pts = 0.0
    feed_health = "unknown"
    quote_age = 999.0
    try:
        hub = get_market_data_hub()
        quote = hub.get_snapshot(epic)
        if quote is not None:
            quote_age = float(quote.age_seconds())
            if quote_age <= 15.0:
                feed_health = "ok"
            elif quote_age <= 45.0:
                feed_health = "degraded"
            else:
                feed_health = "stale"
            bid = float(getattr(quote, "bid", 0) or 0)
            offer = float(getattr(quote, "offer", 0) or 0)
            if bid > 0 and offer > 0:
                spread_pts = offer - bid
                spread_ok, _ = _channel_health_ok(epic, bid, offer, cfg=cfg)
    except Exception:
        pass

    vol_score = min(100.0, tpm * 4.0 + vel * 12.0 + (25.0 if not _in_quiet_center_channel(z) else 0.0))
    spread_score = 100.0 if spread_ok else max(10.0, 60.0 - spread_pts * 10.0)
    feed_score = {"ok": 100.0, "degraded": 55.0, "stale": 20.0}.get(feed_health, 25.0)
    regime, regime_score = _z_fallback_regime(z)
    try:
        from runtime.regime_switch_engine import get_regime_switch_snapshot

        for row in get_regime_switch_snapshot().get("markets") or []:
            if row.get("epic") == epic:
                rs = int(row.get("state") or 2)
                regime = str(row.get("state_label") or regime)
                regime_score = {0: 80.0, 1: 90.0, 2: 35.0}.get(rs, regime_score)
                break
    except Exception:
        pass
    recent_pnl = 0.0
    pnl_score = 50.0

    w_vol, w_spread, w_feed, w_pnl, w_regime = 0.35, 0.20, 0.25, 0.10, 0.10
    try:
        from analytics.tuning_params import get_tuning_params

        tp = get_tuning_params().get("params") or {}
        w_vol = float(tp.get("rotation_weight_volatility", w_vol))
        w_spread = float(tp.get("rotation_weight_spread", w_spread))
        w_feed = float(tp.get("rotation_weight_feed", w_feed))
        w_pnl = float(tp.get("rotation_weight_pnl", w_pnl))
        w_regime = float(tp.get("rotation_weight_regime", w_regime))
    except Exception:
        pass

    composite = round(
        vol_score * w_vol
        + spread_score * w_spread
        + feed_score * w_feed
        + pnl_score * w_pnl
        + regime_score * w_regime,
        2,
    )
    return {
        "epic": epic,
        "label": epic_display_name(epic),
        "composite": composite,
        "volatility": round(vol_score, 2),
        "spread": round(spread_score, 2),
        "feed_health": feed_health,
        "feed_score": round(feed_score, 2),
        "recent_pnl_gbp": round(recent_pnl, 2),
        "pnl_score": round(pnl_score, 2),
        "regime": regime,
        "regime_score": round(regime_score, 2),
        "ticks_per_minute": int(tpm),
        "z_score": round(z, 4),
        "spread_ok": spread_ok,
        "quote_age_sec": round(quote_age, 1) if quote_age < 900 else None,
    }


def _rotation_scores_unlocked(*, cfg: Any | None = None) -> list[dict[str, Any]]:
    rows = [
        _rotation_score_breakdown(epic, cfg=cfg)
        for epic in ROTATION_UNIVERSE
        if epic not in _evicted_epics
    ]
    rows.sort(key=lambda r: float(r.get("composite") or 0), reverse=True)
    return rows


def _record_rotation_history(
    old: tuple[str, ...],
    new: tuple[str, ...],
    *,
    reason: str,
    cfg: Any | None = None,
) -> None:
    """Append rotation event — prefer pre-lock caller with scores snapshot."""
    _rotation_history.append(
        {
            "ts": time.time(),
            "from": list(old),
            "to": list(new),
            "reason": str(reason or "rotation"),
            "scores": {row["epic"]: row for row in _rotation_scores_unlocked(cfg=cfg)},
        }
    )


def _rank_universe_by_velocity(*, cfg: Any | None = None) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    for row in _rotation_scores_unlocked(cfg=cfg):
        scores.append((str(row["epic"]), float(row.get("composite") or 0)))
    if not scores:
        for epic in ROTATION_UNIVERSE:
            if epic in _evicted_epics:
                continue
            vel = float(_velocity_by_epic.get(epic, 0.0))
            tpm = float(_ticks_per_minute(epic))
            snap = _snapshots.get(epic)
            z = float(snap.live_calculated_zscore if snap is not None else 0.0)
            score = vel + tpm * 0.25
            if not _in_quiet_center_channel(z):
                score += 25.0
            if snap is not None and float(snap.updated_at or 0) > 0:
                score += 5.0
            scores.append((epic, score))
        scores.sort(key=lambda row: row[1], reverse=True)
    return scores


def _rotate_active_stack_to(
    epics: tuple[str, ...],
    *,
    reason: str,
    cfg: Any | None = None,
    rest: Any | None = None,
) -> bool:
    """Immediate focus switch — breaks static forex/index lock when channels stagnate."""
    if not multi_source_auto_rotation_enabled(cfg) and is_forex_rotation_locked():
        return False
    global _active_stack_epics, _execution_focus_target, _failover_active, _forex_rotation_locked
    global _failover_state, _failover_reason, _last_rotation_at, _last_rotation_reason
    if rest is None:
        rest = _resolve_stack_rest_client(cfg)
    slots = get_active_stack_slots(cfg)
    tradeable_set = set(resolve_tradeable_stack_epics(rest, cfg))
    cleaned = tuple(
        dict.fromkeys(e for e in epics if e in ROTATION_UNIVERSE and e in tradeable_set)
    )[:slots]
    if not cleaned:
        cleaned = tuple(resolve_tradeable_stack_epics(rest, cfg))[:slots]
    if not cleaned:
        cleaned = DEFAULT_TRADEABLE_STACK[:slots]
    if not cleaned:
        return False
    scores_snapshot = _rotation_scores_unlocked(cfg=cfg)
    with _lock:
        old = tuple(_active_stack_epics)
        if old == cleaned:
            return False
        _active_stack_epics = cleaned
        _execution_focus_target = cleaned[0]
        _last_rotation_at = time.time()
        _last_rotation_reason = str(reason or "rotation")
        if reason == STAGNANT_DEAD_ZONE_REASON or multi_source_auto_rotation_enabled(
            cfg, _lock_held=True
        ):
            _forex_rotation_locked = False
            _failover_active = False
            _failover_state = FAILOVER_STATE_NORMAL
            _failover_reason = str(reason)
        for epic in old:
            _stagnant_since_by_epic.pop(epic, None)
        _rotation_history.append(
            {
                "ts": time.time(),
                "from": list(old),
                "to": list(cleaned),
                "reason": str(reason or "rotation"),
                "scores": {row["epic"]: row for row in scores_snapshot},
            }
        )
    log_engine(
        f"MultiSourceRotation: {list(old)} -> {list(cleaned)} reason={reason}"
    )
    return True


def _rotate_to_high_velocity_stack(
    *,
    reason: str,
    exclude: set[str] | None = None,
    cfg: Any | None = None,
    rest: Any | None = None,
) -> bool:
    exclude = exclude or set()
    if rest is None:
        rest = _resolve_stack_rest_client(cfg)
    tradeable_set = set(resolve_tradeable_stack_epics(rest, cfg))
    ranked = _rank_universe_by_velocity(cfg=cfg)
    slots = get_active_stack_slots(cfg)
    pinned = _epics_with_open_positions()
    picks: list[str] = []
    for epic in pinned:
        if epic in tradeable_set and epic not in exclude and epic not in picks:
            picks.append(epic)
    for epic, _ in ranked:
        if epic not in exclude and epic in tradeable_set and epic not in picks:
            picks.append(epic)
        if len(picks) >= slots:
            break
    picks = picks[:slots]
    if len(picks) < slots:
        for epic in resolve_tradeable_stack_epics(rest, cfg):
            if epic not in picks and epic not in exclude:
                picks.append(epic)
            if len(picks) >= slots:
                break
    if len(picks) < slots:
        for epic in DEFAULT_TRADEABLE_STACK:
            if epic not in picks and epic not in exclude:
                picks.append(epic)
            if len(picks) >= slots:
                break
    return _rotate_active_stack_to(tuple(picks[:slots]), reason=reason, cfg=cfg, rest=rest)


def _resolve_stagnant_dead_zone_sec(cfg: Any | None = None) -> float:
    """Demo throughput overlay may shorten dead-zone rotation (P4)."""
    if cfg is not None:
        try:
            block = cfg.get("demo_throughput_mode") or {}
            if isinstance(block, dict) and block.get("enabled"):
                raw = block.get("stagnant_dead_zone_sec")
                if raw is not None:
                    return max(60.0, float(raw))
        except (TypeError, ValueError):
            pass
    return float(STAGNANT_DEAD_ZONE_SEC)


def _update_stagnant_tracking(
    epic: str,
    z: float,
    *,
    cfg: Any | None = None,
) -> bool:
    """True when epic has floated in quiet center channel for >= dead_zone_sec."""
    key = str(epic or "").strip()
    if not key:
        return False
    now = time.time()
    dead_zone_sec = _resolve_stagnant_dead_zone_sec(cfg)
    # P4 — dead Z + zero velocity: force stagnant sooner (60s)
    if _in_quiet_center_channel(z) and _ticks_per_minute(key) == 0:
        with _lock:
            since = _stagnant_since_by_epic.get(key)
            if since is None:
                _stagnant_since_by_epic[key] = now
                return False
            if (now - since) >= min(60.0, dead_zone_sec):
                return True
    with _lock:
        if _in_quiet_center_channel(z):
            since = _stagnant_since_by_epic.get(key)
            if since is None:
                _stagnant_since_by_epic[key] = now
                return False
            return (now - since) >= dead_zone_sec
        _stagnant_since_by_epic.pop(key, None)
        return False


def bootstrap_multi_source_rotation_stack(*, cfg: Any | None = None) -> dict[str, Any]:
    """Cold-start — pick highest-velocity pair from full night matrix (not forex-only)."""
    if not multi_source_auto_rotation_enabled(cfg):
        return get_rotation_state()
    rest = _resolve_stack_rest_client(cfg)
    hub = get_market_data_hub()
    for epic in ROTATION_UNIVERSE:
        row = _fetch_multi_source_quote(epic, hub, cfg=cfg)
        if row is None:
            continue
        bid, offer, _src, _epoch = row
        ingest_hub_mid(epic, (bid + offer) / 2.0)
    _rotate_to_high_velocity_stack(reason="bootstrap_velocity_scan", cfg=cfg, rest=rest)
    refresh_active_stack_tradeability(cfg=cfg, rest=rest)
    return get_rotation_state()


def evaluate_multi_source_rotation_sweep(*, cfg: Any | None = None) -> dict[str, Any]:
    """
    500ms cross-market sweep — IG hub + Yahoo Finance simultaneously.
    Evicts STAGNANT_DEAD_ZONE channels and rotates to high-velocity alternatives.
    """
    global _rotation_sweep_count, _escape_all_tpm_zero_since, _rotation_escape_active
    rest = _resolve_stack_rest_client(cfg)
    hub = get_market_data_hub()
    stagnant_flags: list[str] = []

    for epic in ROTATION_UNIVERSE:
        try:
            # Hub-snapshot-only ingest: the YahooQuotePoller / stream feeds
            # keep the hub warm, so the 500ms sweep must never do its own
            # network round trips here — 7 sequential Yahoo fetches per pass
            # stretched iterations to ~3s and starved the trading cadence.
            quote = hub.get_snapshot(epic)
            if quote is None:
                continue
            bid = float(getattr(quote, "bid", 0) or 0)
            offer = float(getattr(quote, "offer", 0) or 0)
            age = float(quote.age_seconds())
            if bid <= 0 or offer <= 0 or age > 45.0:
                continue
            _ingest_fresh_quote(
                epic, bid, offer, cfg=cfg,
                source=str(getattr(quote, "source", None) or "hub"),
                quote_time=time.time() - age,
            )
        except Exception as exc:
            log_engine(
                f"MultiSourceRotation: ingest guard {epic_display_name(epic)} "
                f"{type(exc).__name__}: {exc}"
            )

    refresh_active_stack_tradeability_async(cfg=cfg, rest=rest)

    for epic in get_active_stack_epics():
        snap = _snapshots.get(epic)
        z = float(snap.live_calculated_zscore if snap is not None else 0.0)
        if _update_stagnant_tracking(epic, z, cfg=cfg):
            stagnant_flags.append(epic)

    _check_tpm_zero_rehydrate(cfg=cfg)

    pinned = _epics_with_open_positions()
    rotatable_stagnant = [epic for epic in stagnant_flags if epic not in pinned]

    if rotatable_stagnant and multi_source_auto_rotation_enabled(cfg):
        for epic in rotatable_stagnant:
            _evict_epic_from_active_memory(epic, STAGNANT_DEAD_ZONE_REASON)
        _rotate_to_high_velocity_stack(
            reason=STAGNANT_DEAD_ZONE_REASON,
            exclude=set(rotatable_stagnant),
            cfg=cfg,
            rest=rest,
        )

    # Universe escape hatch — all active stack epics have tpm=0 for >60s
    _check_universe_escape_hatch(cfg=cfg)

    with _lock:
        _rotation_sweep_count += 1
        sweep_n = _rotation_sweep_count

    try:
        from system.unified_runtime_state import update_routing

        stack = get_active_stack_epics()
        current = stack[0] if stack else ""
        update_routing(
            rotation_sweep_count=sweep_n,
            current_epic=current,
            rotation_active=True,
        )
    except Exception:
        pass

    try:
        from system.gate_activity import record_gate_evaluation

        for epic in get_active_stack_epics():
            record_gate_evaluation(epic)
    except Exception:
        pass

    return get_rotation_state() | {"stagnant_rotated": stagnant_flags}


def _check_tpm_zero_rehydrate(*, cfg: Any | None = None) -> None:
    """P1 — rehydrate Yahoo/IG stream when all stack epics have tpm=0 for >30s."""
    global _tpm_zero_rehydrate_since
    stack = get_active_stack_epics()
    if not stack:
        _tpm_zero_rehydrate_since = 0.0
        return
    now = time.time()
    all_zero = all(_ticks_per_minute(epic) == 0 for epic in stack)
    if not all_zero:
        _tpm_zero_rehydrate_since = 0.0
        return
    if _tpm_zero_rehydrate_since <= 0.0:
        _tpm_zero_rehydrate_since = now
        return
    if (now - _tpm_zero_rehydrate_since) < TPM_ZERO_REHYDRATE_SEC:
        return
    log_engine(
        f"TpmZeroRehydrate: stack tpm=0 for {(now - _tpm_zero_rehydrate_since):.0f}s — rehydrate"
    )
    _trigger_non_blocking_stream_rehydration(list(stack))
    _tpm_zero_rehydrate_since = now


def _check_universe_escape_hatch(*, cfg: Any | None = None) -> None:
    """Force rehydration + rotation when all active stack epics have tpm=0 for >60s."""
    global _escape_all_tpm_zero_since, _rotation_escape_active
    if not multi_source_auto_rotation_enabled(cfg):
        return
    stack = get_active_stack_epics()
    now = time.time()
    all_zero = all(_ticks_per_minute(epic) == 0 for epic in stack)
    if not all_zero:
        _escape_all_tpm_zero_since = 0.0
        _rotation_escape_active = False
        return
    if _escape_all_tpm_zero_since <= 0.0:
        _escape_all_tpm_zero_since = now
        return
    elapsed = now - _escape_all_tpm_zero_since
    if elapsed < ESCAPE_HATCH_TPM_ZERO_SEC:
        return
    _rotation_escape_active = True
    log_engine(
        f"UniverseEscapeHatch: all stack tpm=0 for {elapsed:.0f}s — forcing rehydration+rotation"
    )
    try:
        _trigger_non_blocking_stream_rehydration(list(stack))
    except Exception as exc:
        log_engine(f"UniverseEscapeHatch: rehydration failed {type(exc).__name__}: {exc}")
    try:
        _rotate_to_high_velocity_stack(reason="escape_hatch_tpm_zero", cfg=cfg)
    except Exception as exc:
        log_engine(f"UniverseEscapeHatch: rotation failed {type(exc).__name__}: {exc}")
    _escape_all_tpm_zero_since = now  # Reset so we retry after another 60s if still stuck


def resolve_max_spread_pts(epic: str, cfg: Any | None = None) -> float:
    """Per-epic spread ceiling from ContractAssetNormalizer + config overlay."""
    from execution.contract_asset_normalizer import resolve_max_spread_pts as _norm_max_spread

    key = str(epic or "").strip()
    try:
        return float(_norm_max_spread(key, cfg))
    except Exception:
        return float(_DEFAULT_MAX_SPREAD_PTS.get(key, 8.0))


def _ticks_per_minute(epic: str) -> int:
    now = time.time()
    arrivals = _tick_arrivals.get(str(epic or "").strip())
    if not arrivals:
        return 0
    return sum(1 for t in arrivals if now - t <= TICK_VELOCITY_WINDOW_SEC)


def ticks_per_window(epic: str, window_sec: float = 10.0) -> int:
    """Tick count inside a rolling window — used by portfolio rotation sweep."""
    now = time.time()
    arrivals = _tick_arrivals.get(str(epic or "").strip())
    if not arrivals:
        return 0
    window = max(1.0, float(window_sec))
    return sum(1 for t in arrivals if now - t <= window)


def _channel_health_ok(epic: str, bid: float, offer: float, cfg: Any | None = None) -> tuple[bool, str]:
    key = str(epic or "").strip()
    if bid <= 0 or offer <= 0:
        return False, "missing_quote"
    spread = float(offer) - float(bid)
    max_spread = resolve_max_spread_pts(key, cfg)
    if spread > max_spread:
        return False, f"spread_exceeds_limit({spread:.4f}>{max_spread})"
    # Boot grace window — skip tpm check for first 180s after stacked tracks start
    now = time.time()
    in_boot_grace = (
        _stacked_tracks_started_at > 0
        and (now - _stacked_tracks_started_at) < BOOT_GRACE_SEC
    )
    if not in_boot_grace:
        tpm = _ticks_per_minute(key)
        if tpm < MIN_TICKS_PER_MINUTE:
            return False, f"tick_velocity_low({tpm}<{MIN_TICKS_PER_MINUTE}/min)"
    return True, ""


def _evict_epic_from_active_memory(epic: str, reason: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _evicted_epics.add(key)
        _evict_deadline_by_epic.pop(key, None)
        _snapshots.pop(key, None)
        _velocity_by_epic.pop(key, None)
        hist = _z_history_by_epic.get(key)
        if hist is not None:
            hist.clear()
    log_engine(f"FailoverStateMachine: evicted {epic_display_name(key)} from active memory ({reason})")


def _activate_forex_failover(reason: str) -> None:
    """Health breach — rotate to high-velocity universe pair (not static forex lock)."""
    if multi_source_auto_rotation_enabled():
        _rotate_to_high_velocity_stack(reason=str(reason or "channel_health_breach"))
        return
    global _failover_state, _failover_active, _failover_reason, _active_stack_epics
    global _execution_focus_target, _ml_sovereignty_active
    with _lock:
        if _failover_active:
            return
        _failover_state = FAILOVER_STATE_FOREX_LOCKED
        _failover_active = True
        _failover_reason = str(reason or "channel_health_breach")
        _active_stack_epics = FAILOVER_FOREX_STACK
        _execution_focus_target = FAILOVER_TOP_EPIC
        _ml_sovereignty_active = True
    log_engine(
        f"FailoverStateMachine: FOREX_FAILOVER locked "
        f"top={FAILOVER_TOP_EPIC} bottom={FAILOVER_BOTTOM_EPIC} reason={_failover_reason}"
    )


def evaluate_failover_tick_health(*, cfg: Any | None = None) -> dict[str, Any]:
    """
    Real-time tick velocity + spread validator.
    Unhealthy channels evicted within EVICT_DEADLINE_SEC; triggers forex failover lock.
    """
    hub = get_market_data_hub()
    now = time.time()
    breaches: list[str] = []

    with _lock:
        stack = tuple(_active_stack_epics)
        already_failover = bool(_failover_active)

    for epic in stack:
        quote = hub.get_snapshot(epic)
        bid = float(quote.bid) if quote is not None else 0.0
        offer = float(quote.offer) if quote is not None else 0.0
        healthy, detail = _channel_health_ok(epic, bid, offer, cfg)
        if healthy:
            with _lock:
                _evict_deadline_by_epic.pop(epic, None)
            continue
        breaches.append(f"{epic_display_name(epic)}:{detail}")
        evict_now = False
        with _lock:
            deadline = _evict_deadline_by_epic.get(epic)
            if deadline is None:
                _evict_deadline_by_epic[epic] = now + EVICT_DEADLINE_SEC
                continue
            if now >= deadline:
                evict_now = True
                _evict_deadline_by_epic.pop(epic, None)
        if evict_now:
            _evict_epic_from_active_memory(epic, detail)

    if not already_failover and breaches:
        _activate_forex_failover("; ".join(breaches))

    return get_failover_state() | {"health_breaches": breaches}


def apply_failover_ml_sovereignty(
    *,
    spread_pts: float,
    slippage_pts: float = 0.0,
    latency_ms: float = 0.0,
) -> dict[str, Any]:
    """When forex failover locked — delegate execution params to ML worker."""
    if not is_forex_failover_active():
        return {}
    try:
        from trading.continuous_optimization_worker import get_continuous_optimization_worker

        worker = get_continuous_optimization_worker()
        return worker.run_failover_sovereignty(
            epic=FAILOVER_TOP_EPIC,
            spread=spread_pts,
            slippage_pts=slippage_pts,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        log_engine(f"FailoverStateMachine: ML sovereignty error {type(exc).__name__}: {exc}")
        return {}


def get_effective_micro_z_threshold() -> float:
    with _lock:
        return float(_ml_dynamic_overrides.get("micro_z_threshold", MICRO_Z_THRESHOLD))


def get_effective_micro_tp_sl() -> tuple[float, float]:
    with _lock:
        tp = float(_ml_dynamic_overrides.get("micro_tp_points", MICRO_TP_POINTS))
        sl = float(_ml_dynamic_overrides.get("micro_sl_points", MICRO_SL_POINTS))
    return tp, sl


def apply_ml_cognitive_overrides(epic: str, overrides: dict[str, Any]) -> None:
    global _ml_dynamic_overrides, _ml_sovereignty_active
    with _lock:
        _ml_dynamic_overrides = dict(overrides)
        _ml_sovereignty_active = True
        _execution_focus_target = str(epic or _execution_focus_target)


def get_execution_focus_state() -> dict[str, Any]:
    channels = get_stacked_asset_channels()
    primary = get_dual_core_snapshot()
    z_stream = get_z_score_stream(get_active_stack_epics()[0] if get_active_stack_epics() else PRIMARY_STACKED_EPIC)
    failover = get_failover_state()
    with _lock:
        if _failover_active:
            focus_label = "EUR/USD + GBP/USD"
            focus_target = FAILOVER_TOP_EPIC
        else:
            focus_label = "Wall Street + Gold"
            focus_target = PRIMARY_STACKED_EPIC
        return {
            "stacked_dual_asset_mode": True,
            "execution_focus_target": focus_target,
            "execution_focus_label": focus_label,
            "focus_tick_velocity": round(float(_focus_tick_velocity), 6),
            "velocity_by_epic": {k: round(v, 6) for k, v in _velocity_by_epic.items()},
            "focus_volatility_z": round(float(primary.volatility_z_score), 4),
            "focus_live_calculated_zscore": round(float(primary.live_calculated_zscore), 4),
            "focus_z_score_stream": z_stream,
            "stacked_asset_channels": channels,
            "ml_strategy_sovereignty": bool(_ml_sovereignty_active),
            "ml_dynamic_params": dict(_ml_dynamic_overrides),
            **failover,
        }


def get_stacked_snapshots() -> dict[str, DualCoreSnapshot]:
    with _lock:
        out: dict[str, DualCoreSnapshot] = {}
        for epic in _active_stack_epics:
            snap = _snapshots.get(epic)
            if snap is not None:
                out[epic] = DualCoreSnapshot(**snap.__dict__)
            elif epic == PRIMARY_STACKED_EPIC and not _failover_active:
                out[epic] = DualCoreSnapshot(**_snapshot.__dict__)
        return out


def get_stacked_asset_channels() -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    stack = get_active_stack_epics()
    roles = ("PRIMARY", "SECONDARY") if not is_forex_failover_active() else ("TOP_SLOT", "BOTTOM_SLOT")
    for idx, epic in enumerate(stack):
        snap = get_stacked_snapshots().get(epic) or get_dual_core_snapshot()
        tpm = _ticks_per_minute(epic)
        channels.append(
            {
                "epic": epic,
                "label": epic_display_name(epic),
                "role": roles[idx] if idx < len(roles) else "STACKED",
                "live_calculated_zscore": round(float(snap.live_calculated_zscore), 4),
                "volatility_z_score": round(float(snap.volatility_z_score), 4),
                "z_score_stream": get_z_score_stream(epic),
                "core_b_micro_active": bool(snap.core_b_micro_active),
                "execution_mode": snap.execution_mode,
                "canary_lot": canary_lot_size(epic),
                "ticks_per_minute": tpm,
                "tick_velocity_ok": tpm >= MIN_TICKS_PER_MINUTE,
            }
        )
    return channels


def get_z_score_stream(epic: str | None = None) -> list[float]:
    key = str(epic or PRIMARY_STACKED_EPIC).strip()
    with _lock:
        hist = _z_history_by_epic.get(key)
        if hist:
            return [round(v, 4) for v in list(hist)]
        return [round(v, 4) for v in list(_z_history)]


def _record_tick_velocity(epic: str, mid: float) -> float:
    """Composite tick velocity — arrivals per 500ms + normalized price impulse."""
    now = time.time()
    key = str(epic or "").strip()
    arrivals = _tick_arrivals.setdefault(key, deque(maxlen=256))
    arrivals.append(now)
    window = CASCADE_SWEEP_SEC
    tick_count = sum(1 for t in arrivals if now - t <= window)
    prev = _last_mid_by_epic.get(key)
    prev_impulse = abs(mid - prev) if prev is not None and prev > 0 else 0.0
    _last_mid_by_epic[key] = mid
    norm = prev_impulse / max(mid, 1e-9)
    return float(tick_count) + norm * 1000.0


def _elect_focus_from_velocity() -> str:
    best_epic = ""
    best_vel = -1.0
    for epic, vel in _velocity_by_epic.items():
        if vel > best_vel:
            best_vel = vel
            best_epic = epic
    return best_epic


_RAW_TICK_DEBUG_LOG = Path("src/data/v31-production/logs/raw_tick_debug.log")
_STRATEGY_EVAL_LOG = Path("src/data/v31-production/logs/strategy_eval.log")


def _ensure_v31_debug_log_dir() -> None:
    try:
        _RAW_TICK_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _append_raw_tick_debug(epic: str, bid: float, ask: float) -> None:
    _ensure_v31_debug_log_dir()
    try:
        with open(_RAW_TICK_DEBUG_LOG, "a") as f:
            f.write(
                f"TIMESTAMP: {time.time()} | TICK ARRIVED FOR {epic} | "
                f"BID: {bid} | ASK: {ask}\n"
            )
    except Exception:
        pass


def append_strategy_eval_log(
    *,
    z_score: float,
    target: float = -2.00,
    block_reason: str = "",
) -> None:
    """500ms strategy loop — raw boolean state to disk for hang diagnosis."""
    _ensure_v31_debug_log_dir()
    try:
        with open(_STRATEGY_EVAL_LOG, "a") as f:
            f.write(
                f"[STRATEGY EVAL] Z-Score: {z_score} | Target: {target:.2f} | "
                f"Match: {z_score <= target} | Valve Block Status: {block_reason}\n"
            )
    except Exception:
        pass


def lite_valve_block_status() -> str:
    """Non-blocking pause/risk latch read — safe on tick-ingest hot path."""
    reasons: list[str] = []
    try:
        from runtime.strategy_kill_switch import is_strategy_kill_active

        if is_strategy_kill_active():
            try:
                from system.config_loader import get_config
                from system.demo_execution_plane import demo_throughput_active

                if not demo_throughput_active(get_config()):
                    reasons.append("BROKER_STATE_MISMATCH")
            except Exception:
                reasons.append("BROKER_STATE_MISMATCH")
    except Exception:
        pass
    try:
        from system.qmm_process_supervisor import process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked and detail:
            reasons.append(detail)
    except Exception:
        pass
    try:
        from api.agent_control import is_paused

        if is_paused():
            reasons.append("api_trading_paused")
    except Exception:
        pass
    try:
        from runtime.deploy_hold import is_deploy_hold_active

        if is_deploy_hold_active():
            reasons.append("deploy_hold_active")
    except Exception:
        pass
    try:
        from runtime.feed_health_watchdog import entries_blocked_by_feed_health

        if entries_blocked_by_feed_health():
            reasons.append("feed_health_unhealthy")
    except Exception:
        pass
    try:
        from system.rest_api_budget import entries_blocked_by_rest_pressure

        blocked, reason = entries_blocked_by_rest_pressure()
        if blocked:
            reasons.append(reason or "rest_pressure_entry_pause")
    except Exception:
        pass
    try:
        # Un-bypassable per-account hard cap (Z6BAH4 → 1 open).
        from execution.order_in_flight_mutex import hard_cap_blocks_entry
        from system.engine_lane import DEFAULT_ACCOUNT_CFD, resolve_journal_metadata
        from system.config_loader import get_config

        cfg = get_config()
        meta = resolve_journal_metadata(cfg=cfg)
        acct = str(meta.get("account_id") or DEFAULT_ACCOUNT_CFD)
        blocked, cap_reason = hard_cap_blocks_entry(acct)
        if blocked:
            reasons.append(cap_reason)
    except Exception:
        pass
    try:
        # Hard snapshot cap — storms cannot recur even when sync/REST starved.
        from runtime.broker_snapshot import open_count_from_snapshot
        from system.config_loader import get_config
        from system.engine_lane import count_cap_for_engine, resolve_active_engine_id

        cfg = get_config()
        engine_id = resolve_active_engine_id(cfg)
        engine_cap = count_cap_for_engine(engine_id, cfg)
        raw_max = getattr(cfg, "max_open_positions", None)
        if engine_cap is not None:
            max_open = int(engine_cap)
        elif raw_max is None:
            max_open = None
        else:
            max_open = max(1, int(raw_max or 6))
        snap_n = open_count_from_snapshot(max_age_sec=300.0)
        if max_open is not None and snap_n is not None and snap_n >= max_open:
            reasons.append(f"broker_snapshot_cap:{snap_n}>={max_open}")
    except Exception:
        pass
    try:
        from pathlib import Path
        import json as _json

        from system.paths import state_dir

        for flag_name, default_reason in (
            ("entry_halt.json", "entry_halt"),
            ("trading_paused.json", "trading_paused"),
        ):
            halt = Path(state_dir()) / flag_name
            if not halt.is_file():
                continue
            raw = _json.loads(halt.read_text(encoding="utf-8"))
            if bool(raw.get("active")):
                reasons.append(str(raw.get("reason") or default_reason))
    except Exception:
        pass
    try:
        from system.config_loader import get_config
        from system.demo_execution_plane import demo_throughput_active

        if not demo_throughput_active(get_config()):
            from system.rest_api_budget import get_rest_api_budget

            if get_rest_api_budget()._preemptive_pause_active():
                reasons.append("rest_budget_preemptive_pause")
    except Exception:
        pass
    return "; ".join(reasons)


def is_api_trading_paused() -> bool:
    try:
        from api.agent_control import is_paused

        return bool(is_paused())
    except Exception:
        return False


ROUTE_LIMIT_CHASE_HF = "limit_chase_hf"
ROUTE_MOMENTUM_BREAKOUT = "momentum_breakout"
ROUTE_MICRO_SCALP_IOC = "micro_scalp_ioc"
LIMIT_CHASE_MAX_TICKS = 3
KELLY_CAP_LIMIT_CHASE = 0.15
KELLY_CAP_MOMENTUM = 0.25
_EXEC_TELEMETRY_MAX = 64

_exec_telemetry_lock = threading.RLock()
_execution_log: deque[dict[str, Any]] = deque(maxlen=_EXEC_TELEMETRY_MAX)
_active_strategy_selections: dict[str, dict[str, Any]] = {}
_chase_state: dict[str, dict[str, Any]] = {}


@dataclass
class StrategyExecutionPlan:
    route: str
    epic: str
    direction: str
    order_type: str
    limit_price: float | None
    size: float
    max_chase_ticks: int
    kelly_cap: float
    approved: bool
    reason: str = ""
    chase_ticks_used: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "epic": self.epic,
            "direction": self.direction,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "size": round(self.size, 4),
            "max_chase_ticks": self.max_chase_ticks,
            "kelly_cap": round(self.kelly_cap, 4),
            "approved": self.approved,
            "reason": self.reason,
            "chase_ticks_used": self.chase_ticks_used,
            "metadata": dict(self.metadata),
        }


def _resolve_tick_size(epic: str, cfg: Any | None = None) -> float:
    try:
        if cfg is not None and hasattr(cfg, "get"):
            instruments = cfg.get("instruments") or {}
            if isinstance(instruments, dict):
                for row in instruments.values():
                    if isinstance(row, dict) and str(row.get("epic") or "") == epic:
                        return max(1e-8, float(row.get("tick_size") or row.get("pip") or 0.0001))
    except Exception:
        pass
    key = str(epic or "").upper()
    if "EURUSD" in key or "GBPUSD" in key:
        return 0.0001
    if "GOLD" in key or "CFPGOLD" in key:
        return 0.1
    if key.startswith("IX.D."):
        return 1.0
    return 0.01


def _record_execution_telemetry(plan: StrategyExecutionPlan) -> None:
    row = {"ts": time.time(), **plan.to_dict()}
    with _exec_telemetry_lock:
        _execution_log.append(row)
        if plan.approved:
            _active_strategy_selections[plan.epic] = row


def get_strategy_execution_telemetry() -> dict[str, Any]:
    with _exec_telemetry_lock:
        return {
            "ok": True,
            "execution_log": list(_execution_log)[-20:],
            "active_selections": dict(_active_strategy_selections),
            "chase_state": {k: dict(v) for k, v in list(_chase_state.items())[:10]},
            "routes": {
                ROUTE_LIMIT_CHASE_HF: {
                    "regime": 0,
                    "kelly_cap": KELLY_CAP_LIMIT_CHASE,
                    "max_chase_ticks": LIMIT_CHASE_MAX_TICKS,
                },
                ROUTE_MOMENTUM_BREAKOUT: {
                    "regime": 1,
                    "kelly_cap": KELLY_CAP_MOMENTUM,
                    "order_type": "MARKET_IOC",
                },
            },
            "ts": time.time(),
        }


def reset_strategy_execution_for_tests() -> None:
    with _exec_telemetry_lock:
        _execution_log.clear()
        _active_strategy_selections.clear()
        _chase_state.clear()


def build_limit_chase_plan(
    *,
    epic: str,
    direction: str,
    bid: float,
    offer: float,
    size: float,
    cfg: Any | None = None,
    z_score: float | None = None,
) -> StrategyExecutionPlan:
    """ROUTE 1 — aggressive limit at best bid (long) / best ask (short), max 3-tick chase."""
    dir_u = str(direction or "BUY").upper()
    tick = _resolve_tick_size(epic, cfg)
    limit_px = float(bid) if dir_u == "BUY" else float(offer)

    try:
        from trading.probability_engine import evaluate_limit_chase_alpha_decay

        alpha = evaluate_limit_chase_alpha_decay(
            epic=epic,
            direction=dir_u,
            expectation_score=float(z_score or 0.55) if z_score is not None else 0.55,
        )
        if bool(alpha.get("kill_order")):
            plan = StrategyExecutionPlan(
                route=ROUTE_LIMIT_CHASE_HF,
                epic=epic,
                direction=dir_u,
                order_type="LIMIT_CANCEL",
                limit_price=limit_px,
                size=size,
                max_chase_ticks=LIMIT_CHASE_MAX_TICKS,
                kelly_cap=KELLY_CAP_LIMIT_CHASE,
                approved=False,
                reason="alpha_decay_kill",
                metadata={"alpha_decay": alpha},
            )
            _record_execution_telemetry(plan)
            return plan
    except Exception:
        pass

    chase_key = f"{epic}:{dir_u}"
    with _exec_telemetry_lock:
        prior = _chase_state.get(chase_key) or {"ticks": 0, "limit": limit_px}
        ticks_used = int(prior.get("ticks") or 0)
        prior_limit = float(prior.get("limit") or limit_px)
        if abs(prior_limit - limit_px) >= tick * 0.5:
            if ticks_used >= LIMIT_CHASE_MAX_TICKS:
                plan = StrategyExecutionPlan(
                    route=ROUTE_LIMIT_CHASE_HF,
                    epic=epic,
                    direction=dir_u,
                    order_type="LIMIT_CANCEL",
                    limit_price=limit_px,
                    size=size,
                    max_chase_ticks=LIMIT_CHASE_MAX_TICKS,
                    kelly_cap=KELLY_CAP_LIMIT_CHASE,
                    approved=False,
                    reason="limit_chase_max_ticks_exceeded",
                    chase_ticks_used=ticks_used,
                    metadata={"bid": bid, "offer": offer, "z_score": z_score},
                )
                _record_execution_telemetry(plan)
                _chase_state.pop(chase_key, None)
                return plan
            ticks_used += 1
        _chase_state[chase_key] = {"ticks": ticks_used, "limit": limit_px, "ts": time.time()}

    plan = StrategyExecutionPlan(
        route=ROUTE_LIMIT_CHASE_HF,
        epic=epic,
        direction=dir_u,
        order_type="LIMIT",
        limit_price=round(limit_px, 8),
        size=size,
        max_chase_ticks=LIMIT_CHASE_MAX_TICKS,
        kelly_cap=KELLY_CAP_LIMIT_CHASE,
        approved=True,
        reason="limit_chase_at_touch",
        chase_ticks_used=ticks_used,
        metadata={"bid": bid, "offer": offer, "tick_size": tick, "z_score": z_score},
    )
    _record_execution_telemetry(plan)
    return plan


def build_momentum_breakout_plan(
    *,
    epic: str,
    direction: str,
    size: float,
    z_score: float | None = None,
) -> StrategyExecutionPlan:
    """ROUTE 2 — IOC market entry on fractal/statistical breakout (regime 1)."""
    dir_u = str(direction or "BUY").upper()
    plan = StrategyExecutionPlan(
        route=ROUTE_MOMENTUM_BREAKOUT,
        epic=epic,
        direction=dir_u,
        order_type="MARKET_IOC",
        limit_price=None,
        size=size,
        max_chase_ticks=0,
        kelly_cap=KELLY_CAP_MOMENTUM,
        approved=True,
        reason="momentum_breakout_ioc",
        metadata={"z_score": z_score, "breakout": True},
    )
    _record_execution_telemetry(plan)
    return plan


def build_micro_scalp_ioc_plan(
    *,
    epic: str,
    direction: str,
    size: float,
    score_pct: float = 0.0,
    forecast_confidence: float = 0.0,
) -> StrategyExecutionPlan:
    """ROUTE 0 — instant predictive micro-scalp MARKET IOC (Core B fast lane)."""
    dir_u = str(direction or "BUY").upper()
    plan = StrategyExecutionPlan(
        route=ROUTE_MICRO_SCALP_IOC,
        epic=epic,
        direction=dir_u,
        order_type="MARKET_IOC",
        limit_price=None,
        size=size,
        max_chase_ticks=0,
        kelly_cap=KELLY_CAP_LIMIT_CHASE,
        approved=True,
        reason="predictive_micro_scalp_ioc",
        metadata={
            "score_pct": score_pct,
            "forecast_confidence": forecast_confidence,
            "bypass_signal_engine": True,
            "order_flow_aligned": True,
        },
    )
    _record_execution_telemetry(plan)
    return plan


def evaluate_strategy_execution(
    *,
    epic: str,
    direction: str,
    bid: float,
    offer: float,
    size: float,
    cfg: Any | None = None,
    z_score: float | None = None,
) -> StrategyExecutionPlan:
    """
    Multi-strategy execution matrix — routes to limit-chase or momentum IOC by orchestrator path.
    Applies portfolio gates (expectation score, correlation, margin freeze) before plan build.
    """
    key = str(epic or "").strip()
    dir_u = str(direction or "BUY").upper()
    try:
        from runtime.portfolio_exploration_engine import passes_strategy_entry_gates

        ok, gate_reason = passes_strategy_entry_gates(
            epic=key,
            direction=dir_u,
            z_score=z_score,
        )
        if not ok:
            plan = StrategyExecutionPlan(
                route="blocked",
                epic=key,
                direction=dir_u,
                order_type="NONE",
                limit_price=None,
                size=size,
                max_chase_ticks=0,
                kelly_cap=0.0,
                approved=False,
                reason=gate_reason or "entry_gate_blocked",
            )
            _record_execution_telemetry(plan)
            return plan
    except Exception as exc:
        # Fail-CLOSED — never build an executable plan when gates throw.
        log_engine(f"StrategyMatrix: gate FAIL-CLOSED {type(exc).__name__}: {exc}")
        plan = StrategyExecutionPlan(
            route="blocked",
            epic=key,
            direction=dir_u,
            order_type="NONE",
            limit_price=None,
            size=size,
            max_chase_ticks=0,
            kelly_cap=0.0,
            approved=False,
            reason=f"entry_gate_fail_closed:{type(exc).__name__}",
        )
        _record_execution_telemetry(plan)
        return plan

    route_path = ROUTE_LIMIT_CHASE_HF
    kelly_cap = KELLY_CAP_LIMIT_CHASE
    try:
        from runtime.master_orchestrator import get_strategy_route

        route = get_strategy_route(key)
        if route:
            route_path = str(route.get("execution_path") or route_path)
            kelly_cap = min(
                float(route.get("kelly_fraction") or kelly_cap),
                KELLY_CAP_MOMENTUM if route_path == ROUTE_MOMENTUM_BREAKOUT else KELLY_CAP_LIMIT_CHASE,
            )
    except Exception:
        pass

    if route_path == ROUTE_MOMENTUM_BREAKOUT:
        plan = build_momentum_breakout_plan(
            epic=key, direction=dir_u, size=size, z_score=z_score
        )
        plan.kelly_cap = min(kelly_cap, KELLY_CAP_MOMENTUM)
        _record_execution_telemetry(plan)
        return plan

    plan = build_limit_chase_plan(
        epic=key,
        direction=dir_u,
        bid=bid,
        offer=offer,
        size=size,
        cfg=cfg,
        z_score=z_score,
    )
    plan.kelly_cap = min(kelly_cap, KELLY_CAP_LIMIT_CHASE)
    return plan


async def _stacked_dual_async_loop(*, cfg: Any | None, interval_sec: float) -> None:
    """Hardened async execution pathway — 500ms non-blocking multi-source strategy sweep."""
    global _rotation_bootstrap_complete
    if not _rotation_bootstrap_complete:
        _rotation_bootstrap_complete = True
        log_engine("MultiSourceRotation: bootstrap deferred to sweep loop ingest")
    await execute_parallel_strategy_sweep(cfg=cfg, stop_event=_stacked_stop, interval_sec=interval_sec)


class ParallelStrategySweepEngine:
    """Non-blocking multi-source sweep — lock-free Z reads, async quote ingest."""

    def __init__(self, *, cfg: Any | None = None) -> None:
        self._cfg = cfg
        self._hub = get_market_data_hub()

    @property
    def active_slots(self) -> tuple[str, ...]:
        return get_active_stack_epics()

    async def get_latest_quote(self, asset_epic: str) -> dict[str, Any] | None:
        try:
            row = await asyncio.to_thread(
                _fetch_multi_source_quote,
                asset_epic,
                self._hub,
                cfg=self._cfg,
            )
            if row is None:
                return None
            bid, offer, source, quote_epoch = row
            mid = (float(bid) + float(offer)) / 2.0
            await asyncio.to_thread(
                _ingest_fresh_quote,
                asset_epic,
                float(bid),
                float(offer),
                cfg=self._cfg,
                source=str(source or "yahoo"),
                quote_time=float(quote_epoch),
            )
            _append_raw_tick_debug(asset_epic, float(bid), float(offer))
            return {
                "bid": float(bid),
                "offer": float(offer),
                "mid": mid,
                # Real quote epoch — the stagnation gate must see hub-quote age,
                # not the fetch wall-clock (which hid stale data as live).
                "timestamp": float(quote_epoch),
                "source": source,
            }
        except RuntimeError:
            # Executor shutdown must propagate so the sweep loop can exit
            # instead of misreading it as a stagnant quote and rotating stacks.
            raise
        except Exception as exc:
            _execution_logger.error(
                "Ingestion guard for %s: %s", asset_epic, exc
            )
            log_engine(
                f"ParallelStrategySweep: ingest guard {epic_display_name(asset_epic)} "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def compute_z_score(self, asset_epic: str) -> float:
        """Volatility Z from snapshot copy — no threading lock on hot path."""
        stacked = get_stacked_snapshots()
        snap = stacked.get(asset_epic)
        if snap is None:
            snap = get_dual_core_snapshot()
        if snap is None or not snap.epic:
            return 0.0
        return float(snap.live_calculated_zscore)

    async def dispatch_unthrottled_order(self, asset_epic: str, z_score: float) -> None:
        await asyncio.to_thread(_dispatch_piercing_zone_order, asset_epic, z_score, self._cfg)


def _broker_gate_for_sweep(asset_epic: str, cfg: Any | None) -> tuple[bool, str]:
    """Blocking broker tradeability gate — runs in a worker thread."""
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import get_shared_rest_client
    from execution.broker_tradeability import broker_new_deal_allowed

    cred = try_load_credentials()
    rest = (
        get_shared_rest_client(cred.credentials)
        if cred.ok and cred.credentials
        else None
    )
    return broker_new_deal_allowed(rest, asset_epic, cfg=cfg)


def _resolve_pierce_thresholds(cfg: Any | None = None) -> tuple[float, float]:
    try:
        from system.demo_execution_plane import demo_pierce_z_threshold

        z = demo_pierce_z_threshold(cfg, default=PIERCE_UPPER_Z)
    except Exception:
        z = PIERCE_UPPER_Z
    return (-float(z), float(z))


async def execute_parallel_strategy_sweep(
    *,
    cfg: Any | None = None,
    stop_event: threading.Event | None = None,
    interval_sec: float = ROTATION_SWEEP_SEC,
) -> None:
    """
    500ms hardened execution loop — multi-source feeds, piercing-zone valve, async yield.
    """
    engine = ParallelStrategySweepEngine(cfg=cfg)
    pierce_lower, pierce_upper = _resolve_pierce_thresholds(cfg)
    while stop_event is None or not stop_event.is_set():
        try:
            # The socket-heartbeat-validator daemon thread already runs
            # validate_socket_heartbeat every second — only duplicate that scan
            # here when the daemon is not alive (belt-and-braces fallback).
            if _heartbeat_thread is None or not _heartbeat_thread.is_alive():
                await asyncio.to_thread(validate_socket_heartbeat)
            await asyncio.to_thread(evaluate_multi_source_rotation_sweep, cfg=cfg)

            slots = tuple(engine.active_slots)
            quote_results = await asyncio.gather(
                *(engine.get_latest_quote(e) for e in slots),
                return_exceptions=True,
            )
            for slot_id, asset_epic in enumerate(slots):
                tick_data = quote_results[slot_id]
                if isinstance(tick_data, BaseException):
                    if isinstance(tick_data, RuntimeError) and "shutdown" in str(tick_data).lower():
                        raise tick_data
                    _execution_logger.error(
                        "Execution loop exception for %s: %s", asset_epic, tick_data
                    )
                    continue
                if not tick_data or time.time() - float(tick_data["timestamp"]) > STAGNANT_QUOTE_SEC:
                    _execution_logger.warning(
                        "Asset %s is stagnant. Triggering failover.", asset_epic
                    )
                    if multi_source_auto_rotation_enabled(cfg):
                        await asyncio.to_thread(
                            _rotate_to_high_velocity_stack,
                            reason=f"stagnant_quote:{asset_epic}",
                            exclude={asset_epic},
                            cfg=cfg,
                        )
                    continue

                z_score = engine.compute_z_score(asset_epic)
                try:
                    append_strategy_eval_log(
                        z_score=z_score,
                        target=pierce_lower,
                        block_reason=lite_valve_block_status(),
                    )
                except Exception:
                    pass

                if z_score <= pierce_lower or z_score >= pierce_upper:
                    try:
                        # Tradeability probe can hit IG REST (cache miss) — keep
                        # that network round trip off the event loop or every
                        # in-flight /api request stalls behind it.
                        ok_trade, trade_reason = await asyncio.to_thread(
                            _broker_gate_for_sweep, asset_epic, cfg
                        )
                        if not ok_trade:
                            set_last_gate_suppression_reason(trade_reason)
                            continue
                    except Exception as exc:
                        set_last_gate_suppression_reason(
                            f"market_status_unavailable:{type(exc).__name__}"
                        )
                        continue
                    _execution_logger.info(
                        "Boundary pierced for %s: Z=%s. Opening Master Valve.",
                        asset_epic,
                        z_score,
                    )
                    log_engine(
                        f"ParallelStrategySweep: piercing zone epic={asset_epic} z={z_score:.4f}"
                    )
                    await engine.dispatch_unthrottled_order(asset_epic, z_score)

            await asyncio.to_thread(evaluate_failover_tick_health, cfg=cfg)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                # Interpreter / event-loop executor is tearing down — exit the
                # sweep instead of spamming the guard every 500ms forever.
                log_engine("ParallelStrategySweep: executor shut down — sweep loop exiting")
                break
            _execution_logger.error("Execution loop exception encountered: %s", exc)
            log_engine(f"ParallelStrategySweep: loop guard RuntimeError: {exc}")
        except Exception as exc:
            _execution_logger.error("Execution loop exception encountered: %s", exc)
            log_engine(
                f"ParallelStrategySweep: loop guard {type(exc).__name__}: {exc}"
            )

        try:
            await asyncio.sleep(max(0.1, float(interval_sec)))
        except (asyncio.CancelledError, RuntimeError):
            break


def _dispatch_piercing_zone_order(epic: str, z_score: float, cfg: Any | None) -> None:
    """Route piercing-zone signal to live coordinator dispatch (sync bridge)."""
    try:
        from runtime.strategy_controller import guard_path_b_handoff

        if not guard_path_b_handoff(epic):
            set_last_gate_suppression_reason("blocked_by_strategy_controller")
            log_engine(
                f"ParallelStrategySweep: blocked_by_strategy_controller epic={epic} "
                f"path=PATH_B_HANDOFF"
            )
            return
    except Exception:
        pass
    try:
        from runtime.hard_enforcement import hard_guard_path_b_handoff, is_hard_enforcement_active

        if not hard_guard_path_b_handoff(epic):
            set_last_gate_suppression_reason("hard_blocked_by_strategy_enforcement")
            log_engine(
                f"ParallelStrategySweep: hard_blocked_by_strategy_enforcement epic={epic} "
                f"path=PATH_B_HANDOFF"
            )
            return
    except Exception:
        pass
    try:
        from runtime.hard_enforcement import is_hard_enforcement_active
        from runtime.strategy_enforcement import soft_guard_path_b_handoff

        if not is_hard_enforcement_active(epic) and not soft_guard_path_b_handoff(epic):
            set_last_gate_suppression_reason("soft_blocked_by_strategy_enforcement")
            log_engine(
                f"ParallelStrategySweep: soft_blocked_by_strategy_enforcement epic={epic} "
                f"path=PATH_B_HANDOFF"
            )
            return
    except Exception:
        pass
    try:
        from runtime.unified_execution import unified_guard_path_b_handoff

        if not unified_guard_path_b_handoff(epic):
            set_last_gate_suppression_reason("blocked_by_unified_execution_route")
            log_engine(
                f"ParallelStrategySweep: blocked_by_unified_execution_route epic={epic} "
                f"path=PATH_B_HANDOFF"
            )
            return
    except Exception:
        pass
    if not epic_allowed_on_hot_path(epic, cfg):
        set_last_gate_suppression_reason("hot_path_epic_excluded")
        log_engine(
            f"ParallelStrategySweep: dispatch blocked epic={epic} reason=hot_path_epic_excluded"
        )
        return
    try:
        from system.strategy_quality_gate import evaluate_entry_hour_gate

        hour_ok, hour_reason, _hour_meta = evaluate_entry_hour_gate(epic, cfg=cfg)
        if not hour_ok:
            set_last_gate_suppression_reason(hour_reason)
            log_engine(
                f"ParallelStrategySweep: hour gate blocked epic={epic} reason={hour_reason}"
            )
            return
    except Exception:
        pass
    try:
        from runtime.entry_rate_limit import check_entry_rate_limit

        rate_ok, rate_reason = check_entry_rate_limit(epic, cfg=cfg)
        if not rate_ok:
            set_last_gate_suppression_reason(rate_reason)
            return
    except Exception:
        pass
    with _sweep_dispatch_lock:
        if is_api_trading_paused():
            set_last_gate_suppression_reason("api_trading_paused")
            return
        direction = "BUY" if float(z_score) <= PIERCE_LOWER_Z else "SELL"
        try:
            hub = get_market_data_hub()
            q = hub.get_quote(epic)
            bid = float(getattr(q, "bid", 0) or 0) if q else 0.0
            offer = float(getattr(q, "offer", 0) or bid) if q else 0.0
            size = float(getattr(cfg, "trade_size", 0.1) if cfg and hasattr(cfg, "trade_size") else 0.1)
            if hasattr(cfg, "get"):
                size = float((cfg.get("trade_size") or size))
            plan = evaluate_strategy_execution(
                epic=epic,
                direction=direction,
                bid=bid,
                offer=offer,
                size=size,
                cfg=cfg,
                z_score=z_score,
            )
            if not plan.approved:
                set_last_gate_suppression_reason(plan.reason)
                log_engine(
                    f"ParallelStrategySweep: strategy matrix blocked epic={epic} reason={plan.reason}"
                )
                return
            from runtime.trade_manager import dispatch_piercing_zone_order

            dispatch_piercing_zone_order(
                epic,
                direction,
                z_score=z_score,
                cfg=cfg,
                execution_plan=plan.to_dict(),
            )
        except Exception as exc:
            set_last_gate_suppression_reason(f"piercing_dispatch_failed:{type(exc).__name__}")
            log_engine(
                f"ParallelStrategySweep: dispatch failed epic={epic}: "
                f"{type(exc).__name__}: {exc}"
            )


def refresh_stacked_dual_assets(*, cfg: Any | None = None) -> dict[str, DualCoreSnapshot | None]:
    """Legacy sync bridge — reads latest stacked snapshots (async loop owns ingest)."""
    results: dict[str, DualCoreSnapshot | None] = {}
    stacked = get_stacked_snapshots()
    for epic in get_active_stack_epics():
        results[epic] = stacked.get(epic)
    return results


def _run_async_poll_loop(*, name: str, coro_factory) -> threading.Thread:
    """Dedicated asyncio thread for non-blocking dual-core poll loops."""

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro_factory())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_runner, name=name, daemon=True)
    thread.start()
    return thread


def refresh_focus_execution_plane() -> DualCoreSnapshot | None:
    """Legacy alias — stacked dual-asset refresh (no single-asset zone-in)."""
    refresh_stacked_dual_assets()
    return get_dual_core_snapshot()


def cognitive_cascade_sweep_once() -> str | None:
    """Legacy alias — stacked refresh returns primary epic."""
    refresh_stacked_dual_assets()
    return PRIMARY_STACKED_EPIC


def _resolve_short_window(cfg: Any | None = None) -> int:
    """Demo throughput may shorten Z warm-up window."""
    if cfg is not None:
        try:
            block = cfg.get("demo_throughput_mode") or {}
            if isinstance(block, dict) and block.get("enabled"):
                raw = block.get("z_short_window")
                if raw is not None:
                    return max(_MIN_SAMPLES, int(raw))
        except (TypeError, ValueError):
            pass
    return _SHORT_WINDOW


def bootstrap_stack_mid_history(cfg: Any | None = None) -> None:
    """G5 pre-warm — seed varied mids so Z-score variance is non-zero immediately."""
    hub = get_market_data_hub()
    short_win = _resolve_short_window(cfg)
    seed_count = max(short_win + 5, 35)
    for epic in ROTATION_UNIVERSE:
        try:
            row = _fetch_multi_source_quote(epic, hub, cfg=cfg)
            if row is None:
                from feeder.yahoo_quote_poller import fetch_yahoo_quote

                sample = fetch_yahoo_quote(epic)
                if sample is None or sample.bid <= 0 or sample.offer <= 0:
                    continue
                bid, offer = float(sample.bid), float(sample.offer)
            else:
                bid, offer, _, _ = row
            spread = max(float(offer) - float(bid), 1e-9)
            mid = (float(bid) + float(offer)) / 2.0
            for i in range(seed_count):
                t = (i / max(seed_count - 1, 1)) - 0.5
                ingest_hub_mid(epic, mid + t * spread, cfg=cfg)
            _mark_fresh_tick(epic)
            _record_quote_pulse(epic)
            try:
                hub.publish(epic, float(bid), float(offer), source="yahoo_prewarm")
            except Exception:
                pass
            log_engine(
                f"BootPrewarm: seeded {epic_display_name(epic)} mid={mid:.5f} "
                f"({seed_count} varied samples)"
            )
        except Exception as exc:
            log_engine(f"BootPrewarm: {epic_display_name(epic)} skipped {type(exc).__name__}: {exc}")


def is_stacked_sweep_thread_alive() -> bool:
    return _stacked_thread is not None and _stacked_thread.is_alive()


_SWEEP_STALL_GRACE_SEC = 12.0


def _stacked_sweep_is_productive() -> bool:
    """Thread alive is insufficient — bootstrap can hang while Gate5 still holds locks."""
    if not is_stacked_sweep_thread_alive():
        return False
    started = float(_stacked_tracks_started_at or 0.0)
    if started <= 0:
        return False
    age = time.time() - started
    if age < 2.0:
        return True
    with _lock:
        sweep = int(_rotation_sweep_count)
    if sweep > 0:
        return True
    return age < _SWEEP_STALL_GRACE_SEC


def _recycle_stacked_sweep_thread(*, reason: str) -> None:
    global _stacked_thread
    log_engine(f"MultiSourceRotation: recycling stacked sweep ({reason})")
    stop_cognitive_cascade()
    _stacked_thread = None
    _stacked_stop.clear()


def _resolve_stacked_cfg(cfg: Any | None) -> Any | None:
    if cfg is not None:
        return cfg
    try:
        from system.config_loader import ConfigLoader

        return ConfigLoader().load(validate=False)
    except Exception:
        return None


def prime_stacked_execution_bootstrap(*, cfg: Any | None = None) -> None:
    """Lightweight pre-G5/post-G5 seed — avoids blocking asyncio thread on Yahoo."""
    global _rotation_bootstrap_complete
    if _rotation_bootstrap_complete:
        return
    resolved = _resolve_stacked_cfg(cfg)
    try:
        from system.feeds.data_feed_orchestrator import primary_feed_active

        if primary_feed_active():
            bootstrap_multi_source_rotation_stack(cfg=resolved)
            _rotation_bootstrap_complete = True
            log_engine("MultiSourceRotation: bootstrap via feed orchestrator")
            return
    except Exception:
        pass
    try:
        bootstrap_stack_mid_history(cfg=resolved)
        bootstrap_multi_source_rotation_stack(cfg=resolved)
        _rotation_bootstrap_complete = True
        log_engine("MultiSourceRotation: bootstrap seed complete")
    except Exception as exc:
        log_engine(
            f"MultiSourceRotation: bootstrap seed skipped {type(exc).__name__}: {exc}"
        )


def _ensure_stacked_sweep_running(*, cfg: Any | None = None) -> None:
    """Restart stacked sweep if thread died or stalled in pre-G5 bootstrap."""
    try:
        from system.system_state import get_system_state

        if not get_system_state().snapshot_model().ready:
            return
    except Exception:
        return
    if _stacked_sweep_is_productive():
        return
    if is_stacked_sweep_thread_alive():
        _recycle_stacked_sweep_thread(reason="stalled_bootstrap")
    else:
        log_engine("MultiSourceRotation: stacked sweep thread dead — restarting")
        global _stacked_thread
        _stacked_thread = None
        _stacked_stop.clear()
    try:
        start_stacked_dual_asset_tracks(cfg=cfg, force=True)
    except Exception as exc:
        log_engine(
            f"MultiSourceRotation: stacked sweep restart failed "
            f"{type(exc).__name__}: {exc}"
        )


def start_stacked_dual_asset_tracks(*, cfg: Any | None = None, force: bool = False) -> None:
    global _stacked_thread, _execution_focus_target, _stacked_tracks_started_at
    if not force and _stacked_sweep_is_productive():
        return
    if is_stacked_sweep_thread_alive():
        _recycle_stacked_sweep_thread(reason="force_rearm" if force else "stale_thread")
    else:
        stop_cognitive_cascade()
        _stacked_stop.clear()
    _execution_focus_target = PRIMARY_STACKED_EPIC
    _stacked_thread = None
    cfg_ref = _resolve_stacked_cfg(cfg)
    poll_sec = float(ROTATION_SWEEP_SEC)

    async def _coro() -> None:
        await _stacked_dual_async_loop(cfg=cfg_ref, interval_sec=poll_sec)

    _stacked_tracks_started_at = time.time()
    _stacked_thread = _run_async_poll_loop(name="stacked-dual-asset", coro_factory=_coro)
    log_engine(
        f"MultiSourceRotation: 500ms sweep armed universe={len(ROTATION_UNIVERSE)} "
        f"poll={poll_sec}s"
    )


def start_cognitive_cascade() -> None:
    """Legacy entry — routes to stacked dual-asset architecture."""
    start_stacked_dual_asset_tracks()


def stop_cognitive_cascade() -> None:
    _stacked_stop.set()


def stop_stacked_dual_asset_tracks() -> None:
    global _stacked_thread, _heartbeat_thread
    stop_cognitive_cascade()
    th = _stacked_thread
    if th is not None and th.is_alive():
        th.join(timeout=3.0)
    _stacked_thread = None
    _stacked_stop.clear()
    _heartbeat_stop.set()
    hb = _heartbeat_thread
    if hb is not None and hb.is_alive():
        hb.join(timeout=2.0)
    _heartbeat_thread = None
    _heartbeat_stop.clear()


def reset_cognitive_cascade_for_tests() -> None:
    global _execution_focus_target, _focus_tick_velocity, _ml_sovereignty_active
    global _failover_state, _failover_active, _failover_reason, _active_stack_epics
    global _forex_rotation_locked, _stagnant_since_by_epic, _last_rotation_at
    global _last_rotation_reason, _rotation_sweep_count, _rotation_bootstrap_complete
    global _stacked_tracks_started_at, _escape_all_tpm_zero_since, _rotation_escape_active
    global _tpm_zero_rehydrate_since
    _stacked_tracks_started_at = 0.0
    _escape_all_tpm_zero_since = 0.0
    _tpm_zero_rehydrate_since = 0.0
    _rotation_escape_active = False
    stop_cognitive_cascade()
    with _lock:
        _execution_focus_target = PRIMARY_STACKED_EPIC
        _focus_tick_velocity = 0.0
        _velocity_by_epic.clear()
        _ml_dynamic_overrides.clear()
        _ml_sovereignty_active = False
        _failover_state = FAILOVER_STATE_NORMAL
        _failover_active = False
        _failover_reason = ""
        _forex_rotation_locked = False
        _active_stack_epics = STACKED_DUAL_ASSETS
        _evicted_epics.clear()
        _evict_deadline_by_epic.clear()
        _stagnant_since_by_epic.clear()
        _last_rotation_at = 0.0
        _last_rotation_reason = ""
        _rotation_sweep_count = 0
        _rotation_bootstrap_complete = False
        _tick_arrivals.clear()
        for epic in ROTATION_UNIVERSE:
            _tick_arrivals[epic] = deque(maxlen=256)
        _z_history.clear()
        _snapshots.clear()
        for hist in _z_history_by_epic.values():
            hist.clear()


def get_dual_core_snapshot() -> DualCoreSnapshot:
    with _lock:
        return DualCoreSnapshot(**_snapshot.__dict__)


def dual_core_status_dict() -> dict[str, Any]:
    snap = get_dual_core_snapshot()
    stacked = get_stacked_snapshots()
    any_micro = any(s.core_b_micro_active for s in stacked.values())
    failover = get_failover_state()
    return {
        **snap.as_dict(),
        "core_b_micro_active": any_micro,
        "engine_b_armed": any_micro,
        "stacked_dual_asset_mode": True,
        "stacked_asset_channels": get_stacked_asset_channels(),
        **failover,
        "dual_core": {
            "label": "⚡ SYSTEM DUAL-CORE STATUS",
            "core_a": {
                "id": "CORE_A",
                "name": "MACRO_BREAKOUT",
                "active": snap.core_a_macro_active,
                "threshold_z": MACRO_Z_THRESHOLD,
            },
            "core_b": {
                "id": "CORE_B",
                "name": "MICRO_SCALPER",
                "active": any_micro,
                "engine": ENGINE_B_MICRO_SCALPER,
                "threshold_z": get_effective_micro_z_threshold(),
            },
        },
    }


def set_last_gate_suppression_reason(reason: str) -> None:
    global _last_gate_suppression_reason
    _last_gate_suppression_reason = str(reason or "").strip()


def get_last_gate_suppression_reason() -> str:
    return _last_gate_suppression_reason


def is_high_conviction_z(z: float) -> bool:
    """True when Z pierces lower zone or sits below zero — high-validity micro setup."""
    zf = float(z)
    return zf <= CORE_B_PIERCE_LOWER_Z or zf < MICRO_HIGH_CONVICTION_Z


def is_piercing_zone_z(z: float) -> bool:
    """
    Open piercing zones — not a strict line match.
    Lower pierce: Z <= -2.00 (deep compression, e.g. -3.4851) arms Core B.
    Upper pierce: Z >= +2.00; interior band [-2, +2] also valid.
    """
    if not CORE_B_FORCE_CHANNEL_OVERRIDE:
        return False
    zf = float(z)
    if zf <= CORE_B_PIERCE_LOWER_Z:
        return True
    if zf >= CORE_B_PIERCE_UPPER_Z:
        return True
    return CORE_B_FORCE_CHANNEL_Z_MIN <= zf <= CORE_B_FORCE_CHANNEL_Z_MAX


def is_force_channel_z(z: float) -> bool:
    """Alias — piercing zone validator (legacy name)."""
    return is_piercing_zone_z(z)


def is_core_b_satellite_uncoupled() -> bool:
    """True when Core B micro-scalper runs outside the 15m macro trend satellite."""
    return bool(DEMO_BYPASS_15M_MACRO_TREND_LOCK and CORE_B_SATELLITE_UNCOUPLED)


def macro_15min_trend_allows_direction(direction: str, epic: str | None = None) -> bool:
    """15m EMA+RSI directional lock — bypassed when Core B satellite is uncoupled."""
    if is_core_b_satellite_uncoupled():
        return True
    trend = resolve_live_15min_macro_trend(epic)
    d = str(direction or "").upper()
    if trend == "BULLISH":
        return d == "BUY"
    if trend == "BEARISH":
        return d == "SELL"
    return trend != "MIXED"


def resolve_core_b_gate_stack() -> dict[str, Any]:
    """
    Live boolean gate matrix for Core B micro-scalper path (GUI diagnostic read-out).

    Gate 3 — stream coupled (hub quote fresh)
    Gate 4 — macro trend protection (uncoupled in test profile)
    Gate 5 — risk netting (process blocks, kill switch, REST budget)
    """
    snap = get_dual_core_snapshot()
    epic = str(snap.epic or PRIMARY_STACKED_EPIC)
    trend = resolve_live_15min_macro_trend(epic)
    uncoupled = is_core_b_satellite_uncoupled()

    stream_ok = True
    stream_detail_parts: list[str] = []
    try:
        hub = get_market_data_hub()
        for stacked_epic in get_active_stack_epics():
            quote = hub.get_snapshot(stacked_epic)
            if quote is None or quote.bid <= 0 or quote.offer <= 0:
                stream_ok = False
                stream_detail_parts.append(f"{epic_display_name(stacked_epic)}=missing")
                continue
            age = round(float(quote.age_seconds()), 2)
            fresh = age <= 45.0
            stream_detail_parts.append(f"{epic_display_name(stacked_epic)} age={age}s")
            if not fresh:
                stream_ok = False
    except Exception:
        stream_ok = False

    if stream_ok:
        g3 = {
            "gate": 3,
            "name": "Stream Coupled",
            "status": "PASSED",
            "detail": " · ".join(stream_detail_parts),
        }
    else:
        g3 = {
            "gate": 3,
            "name": "Stream Coupled",
            "status": "WAITING",
            "detail": " · ".join(stream_detail_parts) or "awaiting stacked quotes",
        }

    if uncoupled:
        g4 = {
            "gate": 4,
            "name": "Macro Trend Protection",
            "status": "UNCOUPLED",
            "detail": f"satellite bypass — 15m={trend} ignored for Core B mean-reversion",
            "macro_trend": trend,
            "blocking": False,
        }
    else:
        buy_ok = macro_15min_trend_allows_direction("BUY", epic)
        sell_ok = macro_15min_trend_allows_direction("SELL", epic)
        if trend == "BEARISH" and not buy_ok:
            g4_status = "BLOCKING"
        elif trend == "BULLISH" and not sell_ok:
            g4_status = "BLOCKING"
        elif trend in ("MIXED", "UNKNOWN"):
            g4_status = "MUTED"
        else:
            g4_status = "PASSED"
        g4 = {
            "gate": 4,
            "name": "Macro Trend Protection",
            "status": g4_status,
            "detail": f"15m macro={trend} buy_ok={buy_ok} sell_ok={sell_ok}",
            "macro_trend": trend,
            "blocking": g4_status == "BLOCKING",
        }

    risk_reasons: list[str] = []
    try:
        from runtime.strategy_kill_switch import is_strategy_kill_active

        if is_strategy_kill_active():
            risk_reasons.append("BROKER_STATE_MISMATCH")
    except Exception:
        pass
    try:
        from system.qmm_process_supervisor import process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked and detail:
            risk_reasons.append(detail)
    except Exception:
        pass
    try:
        from api.agent_control import is_paused

        if is_paused():
            risk_reasons.append("api_trading_paused")
    except Exception:
        pass
    try:
        from system.config_loader import get_config
        from system.demo_execution_plane import demo_throughput_active

        if not demo_throughput_active(get_config()):
            from system.rest_api_budget import get_rest_api_budget

            if get_rest_api_budget()._preemptive_pause_active():
                risk_reasons.append("rest_budget_preemptive_pause")
    except Exception:
        pass

    stacked = get_stacked_snapshots()
    any_micro = any(s.core_b_micro_active for s in stacked.values())
    if not any_micro:
        risk_reasons.append("core_b_not_armed")
    risk_reasons = [
        r for r in risk_reasons if r != "COCKPIT_EMERGENCY_OVERRIDE" and "COCKPIT" not in str(r).upper()
    ]
    hard_blocks = {"BROKER_STATE_MISMATCH", "MASTER_KILL_SWITCH_ACTIVE"}
    if any(r in hard_blocks for r in risk_reasons):
        g5_status = "BLOCKING"
        g5_detail = "; ".join(risk_reasons)
    else:
        g5_status = "PASSED"
        g5_detail = (
            "risk net clear — execution valve open"
            if not risk_reasons
            else f"valve open ({'; '.join(risk_reasons)})"
        )

    g5 = {"gate": 5, "name": "Risk Netting", "status": g5_status, "detail": g5_detail}

    all_clear = (
        g3["status"] == "PASSED"
        and g4["status"] in ("PASSED", "UNCOUPLED")
        and g5["status"] == "PASSED"
    )
    return {
        "core_b_satellite_uncoupled": uncoupled,
        "live_15min_macro_trend": trend,
        "gates": [g3, g4, g5],
        "all_clear": all_clear,
        "summary_lines": [
            f"Gate 3 ({g3['name']}): {g3['status']}",
            f"Gate 4 ({g4['name']}): {g4['status']}",
            f"Gate 5 ({g5['name']}): {g5['status']}",
        ],
    }


def resolve_live_15min_macro_trend(epic: str | None = None) -> str:
    """Current 15m macro bar directional alignment (BULLISH/BEARISH/MIXED/UNKNOWN)."""
    target = str(epic or "").strip()
    if not target:
        target = str(get_dual_core_snapshot().epic or "").strip()
    if not target and NIGHT_MATRIX_EPICS:
        target = NIGHT_MATRIX_EPICS[0]
    try:
        from api.agent_control import get_trading_loop

        orch = get_trading_loop()
        loops = list(getattr(orch, "loops", []) or []) if orch else []
        if not loops:
            return "UNKNOWN"
        for loop in loops:
            loop_epic = str(getattr(loop, "_epic", "") or "")
            market = str(getattr(loop, "_market", "") or "")
            if target and loop_epic and loop_epic != target:
                continue
            se = getattr(loop, "_signal_engine", None)
            if se is None or not market:
                continue
            _, _, c15, _ = se.candle_frames(market)
            if c15 is None or len(c15) < 2:
                continue
            row = c15.iloc[-2]
            if hasattr(se, "add_indicators"):
                c15i = se.add_indicators(c15)
                row = c15i.iloc[-2]
            fast = float(row.get("fast_ema", 0))
            slow = float(row.get("slow_ema", 0))
            if fast > slow:
                return "BULLISH"
            if fast < slow:
                return "BEARISH"
            return "MIXED"
    except Exception:
        pass
    return "UNKNOWN"


def _resolve_mode(z: float) -> tuple[str, bool, bool]:
    zf = float(z)
    if zf >= MACRO_Z_THRESHOLD:
        return MODE_MACRO, True, False
    if is_piercing_zone_z(zf):
        return MODE_MICRO, False, True
    micro_ceiling = get_effective_micro_z_threshold()
    if zf < micro_ceiling:
        return MODE_MICRO, False, True
    return MODE_NEUTRAL, False, False


def _width_reference_stats(widths: deque[float]) -> tuple[float, float] | None:
    """(mean, std) of the PRIOR width window — call before appending the
    current width so the scored value never sits inside its own baseline
    (self-inclusion damped every z by ~sqrt((N-1)/N) plus mean pull)."""
    if len(widths) < _MIN_SAMPLES:
        return None
    arr = np.asarray(widths, dtype=np.float64)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    return mean, max(std, 1e-6)


def _z_score_from_widths(widths: deque[float], current: float) -> float:
    """Compat wrapper — prefer _width_reference_stats on the tick hot path."""
    stats = _width_reference_stats(widths)
    if stats is None:
        return 0.0
    mean, std = stats
    return (current - mean) / std


def ingest_hub_mid(epic: str, mid: float, cfg: Any | None = None) -> DualCoreSnapshot | None:
    """Feed a live mid — updates volatility Z and dual-core mode (non-blocking)."""
    key = str(epic or "").strip()
    if not key or mid <= 0:
        return None
    short_window = _resolve_short_window(cfg)
    try:
        hist = _mid_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
        hist.append(float(mid))
        if len(hist) < short_window:
            return None

        # One deque->array conversion per tick (was two full list copies).
        hist_arr = np.asarray(hist, dtype=np.float64)
        recent = hist_arr[-short_window:]
        rolling = hist_arr[-Z_ROLLING_WINDOW:]
        upper = float(recent.max())
        lower = float(recent.min())
        width = max(upper - lower, 0.0)
        roll_upper = float(rolling.max()) if rolling.size else upper
        roll_lower = float(rolling.min()) if rolling.size else lower
        roll_width = max(roll_upper - roll_lower, 0.0)
        widths = _width_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
        # Reference stats from PRIOR widths only, one numpy pass shared by
        # both scores; append afterwards so the current tick can't dilute
        # its own baseline.
        stats = _width_reference_stats(widths)
        widths.append(width)
        if stats is None:
            z = 0.0
            live_z = 0.0
        else:
            ref_mean, ref_std = stats
            z = (width - ref_mean) / ref_std
            live_z = (
                (roll_width - ref_mean) / ref_std
                if len(rolling) >= _MIN_SAMPLES
                else z
            )
        mode, macro_on, micro_on = _resolve_mode(z)
    except Exception as exc:
        log_engine(
            f"ingest_hub_mid: scoring guard {epic_display_name(key)} "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    snap = DualCoreSnapshot(
        volatility_z_score=z,
        live_calculated_zscore=live_z,
        execution_mode=mode,
        core_a_macro_active=macro_on,
        core_b_micro_active=micro_on,
        engine_b_armed=micro_on,
        micro_channel_upper=upper,
        micro_channel_lower=lower,
        epic=key,
        updated_at=time.time(),
    )
    vel = _record_tick_velocity(key, float(mid))
    with _lock:
        global _snapshot, _focus_tick_velocity
        _velocity_by_epic[key] = vel
        _focus_tick_velocity = vel
        _snapshots[key] = snap
        _z_history_by_epic.setdefault(key, deque(maxlen=_Z_HISTORY_MAX)).append(live_z)
        primary = _active_stack_epics[0] if _active_stack_epics else PRIMARY_STACKED_EPIC
        if key == primary or (key == PRIMARY_STACKED_EPIC and not _failover_active):
            _snapshot = snap
            _z_history.append(live_z)
    try:
        from runtime.virtual_stop_loss import on_streaming_mid_tick

        on_streaming_mid_tick(key, float(mid))
        from runtime.dynamic_limit_engine import on_streaming_mid_tick as on_profit_trail_tick

        on_profit_trail_tick(key, float(mid))
    except Exception:
        pass
    return snap


def refresh_dual_core_from_hub() -> DualCoreSnapshot | None:
    """Poll hub — stacked dual-asset parallel refresh."""
    refresh_stacked_dual_assets()
    return get_dual_core_snapshot()


def _is_fx_epic(epic: str) -> bool:
    e = str(epic or "").upper()
    return "EURUSD" in e or "GBPUSD" in e or ".CFD.IP" in e and "EUR" in e


def canary_lot_size(epic: str, cfg: Any | None = None) -> float:
    """Strict canary clamp — never below IG hard minimum deal sizes."""
    from execution.size_floors import hard_min_deal_size

    e = str(epic or "").upper()
    spreadbet = False
    try:
        from execution.ig_size_validator import fractional_lot_execution_enabled

        spreadbet = fractional_lot_execution_enabled(cfg)
    except Exception:
        spreadbet = False
    index_lot = CANARY_INDEX_LOT_SPREADBET if spreadbet else CANARY_INDEX_LOT
    if "CFPGOLD" in e or "GOLD" in e:
        base = CANARY_GOLD_LOT_SPREADBET if spreadbet else CANARY_GOLD_LOT
    elif "NIKKEI" in e:
        base = max(index_lot, 1.0) if not spreadbet else max(index_lot, CANARY_INDEX_LOT_SPREADBET)
    elif "DOW" in e or "FTSE" in e or "DAX" in e:
        base = index_lot
    elif _is_fx_epic(epic):
        base = CANARY_FX_LOT
    else:
        base = index_lot
    return max(float(base), hard_min_deal_size(epic, cfg=cfg))


def resolve_micro_stop_limit_points(
    rest_client: Any, epic: str, *, size: float = 1.0, cfg: Any | None = None
) -> tuple[float, float]:
    """TP/SL from adaptive ATR bracket (fallback: GBP micro_risk) + broker floors."""
    from execution.live_broker_order_router import floor_stop_distance_points
    from runtime.virtual_stop_loss import stretch_broker_stop_distance

    try:
        snap = get_dual_core_snapshot()
        z = float(snap.volatility_z_score) if snap.epic == epic else None
    except Exception:
        z = None
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    try:
        from execution.adaptive_atr_bracket import resolve_adaptive_entry_bracket

        bracket = resolve_adaptive_entry_bracket(
            epic, "BUY", float(size), cfg, volatility_z=z
        )
        tp_pts, sl_pts = float(bracket.tp_pts), float(bracket.sl_pts)
    except Exception:
        from execution.micro_risk_profile import resolve_micro_tp_sl_for_epic

        tp_pts, sl_pts, _profile = resolve_micro_tp_sl_for_epic(
            epic, size, cfg, volatility_z=z
        )
    tp = floor_stop_distance_points(rest_client, epic, tp_pts).effective_points
    sl = stretch_broker_stop_distance(rest_client, epic, sl_pts)
    return float(tp), float(sl)


def epic_in_stagnant_dead_zone(epic: str, *, cfg: Any | None = None) -> bool:
    """True when dual-core Z-band stagnant dead zone is active for *epic*."""
    key = str(epic or "").strip()
    if not key:
        return False
    snap = _snapshots.get(key)
    z = float(snap.live_calculated_zscore if snap is not None else 0.0)
    return _update_stagnant_tracking(key, z, cfg=cfg)


def evaluate_predictive_micro_scalp_trigger(
    *,
    epic: str,
    bid: float,
    offer: float,
) -> dict[str, Any]:
    """
    Tick-level predictive micro-scalp gate — high-tier micro-trend + OBI alignment.

    Bypasses the 5s SignalEngine stack when Core B instant lane fires.
    """
    empty: dict[str, Any] = {
        "armed": False,
        "direction": None,
        "score_pct": 0.0,
        "promote_tier": "",
        "order_flow_aligned": False,
        "forecast_confidence": 0.0,
        "reason": "not_armed",
    }
    key = str(epic or "").strip()
    if not key or bid <= 0 or offer <= bid:
        empty["reason"] = "invalid_quote"
        return empty

    try:
        from runtime.master_orchestrator import validate_regime_entropy_arbitration

        ok, block_reason = validate_regime_entropy_arbitration(key)
        if not ok:
            empty["reason"] = block_reason
            return empty
    except Exception:
        pass

    try:
        from apex.microkernel import get_microkernel

        mt = get_microkernel().micro_trend_for(key)
    except Exception:
        mt = {}

    score = float(mt.get("score_pct") or 0.0)
    tier = str(mt.get("promote_tier") or "")
    direction = str(mt.get("direction") or "FLAT")
    flow_aligned = bool(mt.get("order_flow_aligned"))
    forecast_conf = float(mt.get("forecast_confidence") or 0.0)

    if tier != "high" or score < MICRO_SCALP_INSTANT_MIN_SCORE:
        empty.update(
            {
                "score_pct": score,
                "promote_tier": tier,
                "order_flow_aligned": flow_aligned,
                "forecast_confidence": forecast_conf,
                "reason": "micro_trend_not_high_tier",
            }
        )
        return empty

    if direction not in ("BUY", "SELL"):
        empty["reason"] = "direction_flat"
        return empty

    # Depthless Yahoo books report order_flow_aligned=False with OBI≈0 forever.
    # Only hard-block when OBI is informative against the side (|obi| ≥ epic threshold).
    obi_ratio = float(mt.get("obi_ratio") or 0.0)
    obi_thr = 0.22
    try:
        from system.memory_context import resolve_asset_profile

        obi_thr = float(resolve_asset_profile(str(epic or "")).obi_threshold or 0.22)
    except Exception:
        obi_thr = 0.22
    if not flow_aligned and abs(obi_ratio) >= obi_thr:
        empty.update(
            {
                "score_pct": score,
                "promote_tier": tier,
                "order_flow_aligned": False,
                "forecast_confidence": forecast_conf,
                "reason": "obi_not_aligned",
            }
        )
        return empty

    return {
        "armed": True,
        "direction": direction,
        "score_pct": score,
        "promote_tier": tier,
        "order_flow_aligned": True,
        "forecast_confidence": forecast_conf,
        "reason": "predictive_micro_scalp_armed",
        "target_min_pts": MICRO_SCALP_TARGET_MIN_PTS,
        "target_max_pts": MICRO_SCALP_TARGET_MAX_PTS,
        "bypass_signal_engine": True,
    }


def _resolve_instant_scalp_cadence_sec(cfg: Any | None = None) -> float:
    """Event-driven tick lane: zero cadence (raw WS). Optional floor from config."""
    try:
        if cfg is not None and hasattr(cfg, "get"):
            block = cfg.get("event_driven_tick") or {}
            if isinstance(block, dict) and bool(block.get("enabled", True)):
                return max(0.0, float(block.get("min_entry_interval_sec") or 0.0))
    except Exception:
        pass
    return float(MICRO_SCALP_INSTANT_CADENCE_SEC)


def try_instant_predictive_micro_scalp(
    epic: str,
    bid: float,
    offer: float,
    *,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Execute hyper-fast IOC/FOK limit micro-scalp on raw tick (no TWMA lag)."""
    key = str(epic or "").strip()
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None

    # Absolute front of pipeline — kill before trigger/order/network work.
    try:
        from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_veto

        # Direction unknown until trigger; use BUY as conservative crash-guard
        # then re-check with real direction below.
        ok_pre, pre_reason = evaluate_pre_entry_regime_veto(
            key, "BUY", bid=bid, offer=offer, cfg=cfg
        )
        # Spread% veto is direction-agnostic; OBI crash guard on BUY is the
        # strict default for long-biased desk. Full direction check after arm.
        if not ok_pre and "spread" in pre_reason:
            set_last_gate_suppression_reason(pre_reason)
            return {"dispatched": False, "reason": pre_reason, "trigger": {}}
    except Exception as exc:
        reason = f"regime_veto_fail_closed:{type(exc).__name__}"
        set_last_gate_suppression_reason(reason)
        return {"dispatched": False, "reason": reason, "trigger": {}}

    trigger = evaluate_predictive_micro_scalp_trigger(epic=epic, bid=bid, offer=offer)
    if not trigger.get("armed"):
        return {"dispatched": False, "trigger": trigger}

    direction = str(trigger["direction"])
    try:
        from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_veto

        ok_dir, dir_reason = evaluate_pre_entry_regime_veto(
            key, direction, bid=bid, offer=offer, cfg=cfg
        )
        if not ok_dir:
            set_last_gate_suppression_reason(dir_reason)
            return {"dispatched": False, "trigger": trigger, "reason": dir_reason}
    except Exception as exc:
        reason = f"regime_veto_fail_closed:{type(exc).__name__}"
        set_last_gate_suppression_reason(reason)
        return {"dispatched": False, "trigger": trigger, "reason": reason}

    now = time.time()
    with _micro_scalper_lane_lock:
        last = _last_instant_scalp_at.get(key, 0.0)
        cadence = _resolve_instant_scalp_cadence_sec(cfg)
        if cadence > 0 and now - last < cadence:
            return {
                "dispatched": False,
                "trigger": trigger,
                "reason": "instant_scalp_cadence",
            }
        _last_instant_scalp_at[key] = now

    if not epic_allowed_on_hot_path(key, cfg):
        return {"dispatched": False, "trigger": trigger, "reason": "hot_path_excluded"}

    size = float(getattr(cfg, "trade_size", 0.1) if cfg and hasattr(cfg, "trade_size") else 0.1)
    if cfg is not None and hasattr(cfg, "get"):
        size = float((cfg.get("trade_size") or size))

    try:
        from runtime.portfolio_exploration_engine import passes_strategy_entry_gates

        ok, gate_reason = passes_strategy_entry_gates(epic=key, direction=direction)
        if not ok:
            set_last_gate_suppression_reason(gate_reason)
            return {"dispatched": False, "trigger": trigger, "reason": gate_reason}
    except Exception as exc:
        return {"dispatched": False, "trigger": trigger, "reason": f"gate_check_{type(exc).__name__}"}

    # Enqueue only — do NOT acquire/redeem here. Premature fast-pass redeem
    # burned vouchers on cadence no-ops and stole ig_orders refill so the wire
    # POST (/positions/otc) waited 30s then failed token-bucket exhausted.
    try:
        from system.chaos_guardian import enqueue_fast_pass_token

        enqueue_fast_pass_token(
            epic=key,
            direction=direction,
            score=float(trigger.get("score_pct") or 0.0),
            reason="predictive_micro_scalp",
        )
    except Exception as exc:
        return {"dispatched": False, "trigger": trigger, "reason": f"fast_pass_{type(exc).__name__}"}

    plan = build_micro_scalp_ioc_plan(
        epic=key,
        direction=direction,
        size=size,
        score_pct=float(trigger.get("score_pct") or 0.0),
        forecast_confidence=float(trigger.get("forecast_confidence") or 0.0),
    )
    if not plan.approved:
        set_last_gate_suppression_reason(plan.reason)
        return {"dispatched": False, "trigger": trigger, "reason": plan.reason}

    plan_dict = plan.to_dict()
    plan_dict["micro_target_min_pts"] = MICRO_SCALP_TARGET_MIN_PTS
    plan_dict["micro_target_max_pts"] = MICRO_SCALP_TARGET_MAX_PTS
    plan_dict["instant_scalp_lane"] = True
    plan_dict["bypass_signal_engine"] = True
    plan_dict["predictive_score_pct"] = float(trigger.get("score_pct") or 0.0)
    plan_dict["forecast_confidence"] = float(trigger.get("forecast_confidence") or 0.0)

    try:
        from runtime.trade_manager import dispatch_piercing_zone_order

        dispatch_piercing_zone_order(
            key,
            direction,
            z_score=0.0,
            cfg=cfg,
            execution_plan=plan_dict,
        )
        log_engine(
            f"MicroScalperInstant: {direction} epic={key} score={trigger.get('score_pct'):.1f} "
            f"forecast={trigger.get('forecast_confidence'):.2f} "
            f"target={MICRO_SCALP_TARGET_MIN_PTS}-{MICRO_SCALP_TARGET_MAX_PTS}pt"
        )
        return {"dispatched": True, "trigger": trigger, "plan": plan_dict}
    except Exception as exc:
        set_last_gate_suppression_reason(f"instant_scalp_failed:{type(exc).__name__}")
        return {
            "dispatched": False,
            "trigger": trigger,
            "reason": f"{type(exc).__name__}:{exc}",
        }


def _on_hub_quote_micro_scalper_lane(snap: Any) -> None:
    """Streaming tick callback — decoupled from 500ms REST poll sweep."""
    try:
        epic = str(getattr(snap, "epic", "") or "")
        bid = float(getattr(snap, "bid", 0) or 0)
        offer = float(getattr(snap, "offer", 0) or bid)
        if not epic or bid <= 0 or offer <= bid:
            return
        try:
            from apex.microkernel import get_microkernel

            get_microkernel().on_tick_ingest(epic, snap)
        except Exception:
            pass
        try_instant_predictive_micro_scalp(epic, bid, offer)
    except Exception as exc:
        log_engine(f"MicroScalperInstant: hub tick guard {type(exc).__name__}: {exc}")


def start_micro_scalper_tick_lane() -> bool:
    """Register hub quote listener for tick-level Core B instant scalps."""
    global _micro_scalper_lane_registered, _micro_scalper_lane_unsub
    with _micro_scalper_lane_lock:
        if _micro_scalper_lane_registered:
            return True
        try:
            hub = get_market_data_hub()
            _micro_scalper_lane_unsub = hub.on_quote(_on_hub_quote_micro_scalper_lane)
            _micro_scalper_lane_registered = True
            log_engine("MicroScalperInstant: tick lane registered on hub quote stream")
            return True
        except Exception as exc:
            log_engine(
                f"MicroScalperInstant: tick lane registration failed "
                f"{type(exc).__name__}: {exc}"
            )
            return False


def stop_micro_scalper_tick_lane() -> None:
    """Unregister hub quote listener."""
    global _micro_scalper_lane_registered, _micro_scalper_lane_unsub
    with _micro_scalper_lane_lock:
        if _micro_scalper_lane_unsub is not None:
            try:
                _micro_scalper_lane_unsub()
            except Exception:
                pass
            _micro_scalper_lane_unsub = None
        _micro_scalper_lane_registered = False


def reset_micro_scalper_tick_lane_for_tests() -> None:
    stop_micro_scalper_tick_lane()
    with _micro_scalper_lane_lock:
        _last_instant_scalp_at.clear()


def evaluate_micro_scalp_signal(
    *,
    epic: str,
    bid: float,
    offer: float,
    snap: DualCoreSnapshot | None = None,
) -> str | None:
    """
    Mean-reversion: SELL at upper micro-channel, BUY at lower micro-channel.
    Returns 'BUY' | 'SELL' | None.
    """
    snap = snap or get_dual_core_snapshot()
    if not snap.core_b_micro_active or snap.epic != epic:
        set_last_gate_suppression_reason("core_b_inactive_or_epic_mismatch")
        return None
    if snap.micro_channel_upper is None or snap.micro_channel_lower is None:
        set_last_gate_suppression_reason("micro_channel_uninitialized")
        return None
    mid = (bid + offer) / 2.0
    span = max(snap.micro_channel_upper - snap.micro_channel_lower, 1e-9)
    z = float(snap.volatility_z_score)
    if is_force_channel_z(z):
        center = (snap.micro_channel_upper + snap.micro_channel_lower) / 2.0
        direction = "SELL" if mid >= center else "BUY"
        set_last_gate_suppression_reason("")
        return direction
    touch_pct = 0.08
    if is_high_conviction_z(z):
        touch_pct = 0.40
    elif DEMO_BYPASS_15M_MACRO_TREND_LOCK:
        touch_pct = 0.22
    touch = span * touch_pct
    direction: str | None = None
    if mid >= snap.micro_channel_upper - touch:
        direction = "SELL"
    elif mid <= snap.micro_channel_lower + touch:
        direction = "BUY"
    elif DEMO_BYPASS_15M_MACRO_TREND_LOCK and z < get_effective_micro_z_threshold():
        center = (snap.micro_channel_upper + snap.micro_channel_lower) / 2.0
        direction = "SELL" if mid >= center else "BUY"
    if direction is None:
        set_last_gate_suppression_reason("awaiting_micro_channel_touch")
        return None
    if not is_core_b_satellite_uncoupled():
        if not macro_15min_trend_allows_direction(direction, epic):
            set_last_gate_suppression_reason("15m_macro_trend_lock")
            return None
    set_last_gate_suppression_reason("")
    return direction


def reset_dual_core_for_tests() -> None:
    with _lock:
        global _snapshot
        _snapshot = DualCoreSnapshot(
            volatility_z_score=0.0,
            live_calculated_zscore=0.0,
            execution_mode=MODE_NEUTRAL,
            core_a_macro_active=False,
            core_b_micro_active=False,
            engine_b_armed=False,
            micro_channel_upper=None,
            micro_channel_lower=None,
            epic="",
        )
    _mid_history.clear()
    _width_history.clear()
    _z_history.clear()
    _snapshots.clear()
    for hist in _z_history_by_epic.values():
        hist.clear()
    reset_cognitive_cascade_for_tests()
    reset_micro_scalper_tick_lane_for_tests()
    global _last_gate_suppression_reason
    _last_gate_suppression_reason = ""
