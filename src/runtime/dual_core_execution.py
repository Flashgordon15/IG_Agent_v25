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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

_execution_logger = logging.getLogger("ig_agent.parallel_strategy_sweep")
PIERCE_LOWER_Z = -2.00
PIERCE_UPPER_Z = 2.00
STAGNANT_QUOTE_SEC = 10.0
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
CANARY_INDEX_LOT = 0.5
CANARY_GOLD_LOT = 1.0
PRIMARY_STACKED_EPIC = "IX.D.DOW.IFM.IP"
SECONDARY_STACKED_EPIC = "CS.D.CFPGOLD.CFP.IP"
STACKED_DUAL_ASSETS: tuple[str, ...] = (PRIMARY_STACKED_EPIC, SECONDARY_STACKED_EPIC)
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


def multi_source_auto_rotation_enabled(
    cfg: Any | None = None,
    *,
    _lock_held: bool = False,
) -> bool:
    """False when config or runtime forex lock pins EUR/USD + GBP/USD hot path."""
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict) and dual.get("forex_rotation_locked"):
                return False
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


def epic_allowed_on_hot_path(epic: str, cfg: Any | None = None) -> bool:
    """Reject dispatch on epics outside active stack or on exclude list."""
    key = str(epic or "").strip()
    if not key:
        return False
    if key not in get_active_stack_epics():
        return False
    try:
        if cfg is not None and hasattr(cfg, "get"):
            dual = cfg.get("dual_core") or {}
            if isinstance(dual, dict):
                excluded = {str(e).strip() for e in (dual.get("exclude_from_hot_path") or [])}
                if key in excluded:
                    return False
    except Exception:
        pass
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
_stacked_stop = threading.Event()
_stacked_thread: threading.Thread | None = None

# Socket heartbeat — stale stream detection + non-blocking rehydration
SOCKET_STALE_SEC = 5.0
_last_fresh_tick_at: dict[str, float] = {}
_socket_channel_state: dict[str, str] = {}
_stream_reset_inflight = False
_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None


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


def _trigger_non_blocking_stream_rehydration(stale_epics: list[str]) -> None:
    """Reset Yahoo/IG feed without blocking the scoring loop thread."""
    global _stream_reset_inflight
    if not stale_epics:
        return
    with _lock:
        if _stream_reset_inflight:
            return
        _stream_reset_inflight = True

    def _worker() -> None:
        global _stream_reset_inflight
        try:
            log_engine(
                f"SocketHeartbeat: SOCKET_STALE on {stale_epics} — non-blocking rehydrate"
            )
            from feeder.yahoo_quote_poller import start_yahoo_quote_poller, stop_yahoo_quote_poller
            from feeder.pricing_transport import yahoo_poll_seconds
            from system.stream_ready import reset_stream_ready

            reset_stream_ready()
            stop_yahoo_quote_poller()

            async def _pause_yield() -> None:
                await asyncio.sleep(0.25)

            asyncio.run(_pause_yield())
            epics = list(
                dict.fromkeys(
                    list(get_active_stack_epics()) + list(NIGHT_MATRIX_EPICS)
                )
            )
            cfg = None
            try:
                from system.config_loader import ConfigLoader

                cfg = ConfigLoader().load()
            except Exception:
                pass
            start_yahoo_quote_poller(epics, poll_sec=yahoo_poll_seconds(cfg))
            try:
                from runtime.agent_bootstrap import start_market_stream
                from system.credentials_loader import try_load_credentials
                from system.ig_rest_session import get_shared_rest_client

                cred = try_load_credentials()
                if cred.ok and cred.credentials is not None and cfg is not None:
                    start_market_stream(
                        cfg,
                        rest_client=get_shared_rest_client(cred.credentials),
                        clear_stream_ready=False,
                    )
            except Exception as exc:
                log_engine(
                    f"SocketHeartbeat: IG stream re-arm skipped {type(exc).__name__}: {exc}"
                )
            for epic in stale_epics:
                _mark_fresh_tick(epic)
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
            _mark_fresh_tick(epic)
            continue
        last = float(_last_fresh_tick_at.get(epic) or 0.0)
        if last <= 0.0 or (now - last) > SOCKET_STALE_SEC:
            stale_epics.append(epic)
            with _lock:
                _socket_channel_state[epic] = "SOCKET_STALE"
    if stale_epics:
        _trigger_non_blocking_stream_rehydration(stale_epics)
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
    with _lock:
        return tuple(_active_stack_epics)


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

        if cfg is None:
            cfg = load_active_config(validate=False)
        forex_stack = resolve_hot_path_epics_from_config(cfg, rest=rest)
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
    with _lock:
        return get_rotation_state_locked()


def get_rotation_state_locked() -> dict[str, Any]:
    """Caller must hold ``_lock``."""
    stagnant: dict[str, float] = {}
    now = time.time()
    for epic, since in _stagnant_since_by_epic.items():
        stagnant[epic] = round(max(0.0, now - since), 1)
    return {
        "multi_source_auto_rotation": multi_source_auto_rotation_enabled(_lock_held=True),
        "rotation_universe": list(ROTATION_UNIVERSE),
        "rotation_sweep_sec": ROTATION_SWEEP_SEC,
        "last_rotation_at": _last_rotation_at,
        "last_rotation_reason": _last_rotation_reason,
        "rotation_sweep_count": _rotation_sweep_count,
        "stagnant_dead_zone_epics": stagnant,
        "stagnant_z_band": [STAGNANT_Z_MIN, STAGNANT_Z_MAX],
        "stagnant_dead_zone_sec": STAGNANT_DEAD_ZONE_SEC,
    }


def _in_quiet_center_channel(z: float) -> bool:
    return STAGNANT_Z_MIN <= float(z) <= STAGNANT_Z_MAX


def _fetch_multi_source_quote(
    epic: str,
    hub: Any,
    *,
    cfg: Any | None = None,
) -> tuple[float, float, str] | None:
    """Hub (IG/stream) first, Yahoo Finance fallback — 3-source cross-market scan."""
    key = str(epic or "").strip()
    if not key:
        return None
    quote = hub.get_snapshot(key)
    if quote is not None and float(getattr(quote, "bid", 0) or 0) > 0:
        offer = float(getattr(quote, "offer", 0) or 0)
        if offer > 0 and float(quote.age_seconds()) <= 45.0:
            return float(quote.bid), offer, str(getattr(quote, "source", None) or "hub")
    try:
        from feeder.yahoo_quote_poller import fetch_yahoo_quote

        sample = fetch_yahoo_quote(key)
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
            return float(sample.bid), float(sample.offer), "yahoo"
    except Exception:
        pass
    if quote is not None and float(getattr(quote, "bid", 0) or 0) > 0:
        offer = float(getattr(quote, "offer", 0) or 0)
        if offer > 0:
            return float(quote.bid), offer, str(getattr(quote, "source", None) or "hub_stale")
    return None


def _rank_universe_by_velocity(*, cfg: Any | None = None) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
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


def _rotate_active_stack_to(epics: tuple[str, ...], *, reason: str, cfg: Any | None = None) -> bool:
    """Immediate focus switch — breaks static forex/index lock when channels stagnate."""
    if not multi_source_auto_rotation_enabled(cfg) and is_forex_rotation_locked():
        return False
    global _active_stack_epics, _execution_focus_target, _failover_active, _forex_rotation_locked
    global _failover_state, _failover_reason, _last_rotation_at, _last_rotation_reason
    cleaned = tuple(dict.fromkeys(e for e in epics if e in ROTATION_UNIVERSE))[
        :ACTIVE_STACK_SLOTS
    ]
    if not cleaned:
        return False
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
    log_engine(
        f"MultiSourceRotation: {list(old)} -> {list(cleaned)} reason={reason}"
    )
    return True


def _rotate_to_high_velocity_stack(
    *,
    reason: str,
    exclude: set[str] | None = None,
    cfg: Any | None = None,
) -> bool:
    exclude = exclude or set()
    ranked = _rank_universe_by_velocity(cfg=cfg)
    picks = [epic for epic, _ in ranked if epic not in exclude][:ACTIVE_STACK_SLOTS]
    if len(picks) < ACTIVE_STACK_SLOTS:
        for epic in ROTATION_UNIVERSE:
            if epic not in picks and epic not in exclude:
                picks.append(epic)
            if len(picks) >= ACTIVE_STACK_SLOTS:
                break
    return _rotate_active_stack_to(tuple(picks[:ACTIVE_STACK_SLOTS]), reason=reason, cfg=cfg)


def _update_stagnant_tracking(epic: str, z: float) -> bool:
    """True when epic has floated in quiet center channel for >= STAGNANT_DEAD_ZONE_SEC."""
    key = str(epic or "").strip()
    if not key:
        return False
    now = time.time()
    with _lock:
        if _in_quiet_center_channel(z):
            since = _stagnant_since_by_epic.get(key)
            if since is None:
                _stagnant_since_by_epic[key] = now
                return False
            return (now - since) >= STAGNANT_DEAD_ZONE_SEC
        _stagnant_since_by_epic.pop(key, None)
        return False


def bootstrap_multi_source_rotation_stack(*, cfg: Any | None = None) -> dict[str, Any]:
    """Cold-start — pick highest-velocity pair from full night matrix (not forex-only)."""
    if not multi_source_auto_rotation_enabled(cfg):
        return get_rotation_state()
    hub = get_market_data_hub()
    for epic in ROTATION_UNIVERSE:
        row = _fetch_multi_source_quote(epic, hub, cfg=cfg)
        if row is None:
            continue
        bid, offer, _src = row
        ingest_hub_mid(epic, (bid + offer) / 2.0)
    _rotate_to_high_velocity_stack(reason="bootstrap_velocity_scan", cfg=cfg)
    return get_rotation_state()


def evaluate_multi_source_rotation_sweep(*, cfg: Any | None = None) -> dict[str, Any]:
    """
    500ms cross-market sweep — IG hub + Yahoo Finance simultaneously.
    Evicts STAGNANT_DEAD_ZONE channels and rotates to high-velocity alternatives.
    """
    global _rotation_sweep_count
    hub = get_market_data_hub()
    stagnant_flags: list[str] = []

    for epic in ROTATION_UNIVERSE:
        try:
            row = _fetch_multi_source_quote(epic, hub, cfg=cfg)
            if row is None:
                continue
            bid, offer, _src = row
            mid = (float(bid) + float(offer)) / 2.0
            snap = ingest_hub_mid(epic, mid)
            if snap is not None:
                _mark_fresh_tick(epic)
        except Exception as exc:
            log_engine(
                f"MultiSourceRotation: ingest guard {epic_display_name(epic)} "
                f"{type(exc).__name__}: {exc}"
            )

    for epic in get_active_stack_epics():
        snap = _snapshots.get(epic)
        z = float(snap.live_calculated_zscore if snap is not None else 0.0)
        if _update_stagnant_tracking(epic, z):
            stagnant_flags.append(epic)

    if stagnant_flags and multi_source_auto_rotation_enabled(cfg):
        for epic in stagnant_flags:
            _evict_epic_from_active_memory(epic, STAGNANT_DEAD_ZONE_REASON)
        _rotate_to_high_velocity_stack(
            reason=STAGNANT_DEAD_ZONE_REASON,
            exclude=set(stagnant_flags),
            cfg=cfg,
        )

    with _lock:
        _rotation_sweep_count += 1

    return get_rotation_state() | {"stagnant_rotated": stagnant_flags}


def resolve_max_spread_pts(epic: str, cfg: Any | None = None) -> float:
    """Per-epic spread ceiling from config overlay or sensible defaults."""
    key = str(epic or "").strip()
    if cfg is not None:
        try:
            markets = cfg.get("markets", {}) if hasattr(cfg, "get") else {}
            if isinstance(markets, dict):
                for _mk, row in markets.items():
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("epic") or "") == key:
                        return float(row.get("max_spread_pts") or _DEFAULT_MAX_SPREAD_PTS.get(key, 8.0))
        except Exception:
            pass
    return float(_DEFAULT_MAX_SPREAD_PTS.get(key, 8.0))


def _ticks_per_minute(epic: str) -> int:
    now = time.time()
    arrivals = _tick_arrivals.get(str(epic or "").strip())
    if not arrivals:
        return 0
    return sum(1 for t in arrivals if now - t <= TICK_VELOCITY_WINDOW_SEC)


def _channel_health_ok(epic: str, bid: float, offer: float, cfg: Any | None = None) -> tuple[bool, str]:
    key = str(epic or "").strip()
    if bid <= 0 or offer <= 0:
        return False, "missing_quote"
    spread = float(offer) - float(bid)
    max_spread = resolve_max_spread_pts(key, cfg)
    if spread > max_spread:
        return False, f"spread_exceeds_limit({spread:.4f}>{max_spread})"
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


async def _stacked_dual_async_loop(*, cfg: Any | None, interval_sec: float) -> None:
    """Hardened async execution pathway — 500ms non-blocking multi-source strategy sweep."""
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
            bid, offer, source = row
            now = time.time()
            mid = (float(bid) + float(offer)) / 2.0
            await asyncio.to_thread(ingest_hub_mid, asset_epic, mid)
            _append_raw_tick_debug(asset_epic, float(bid), float(offer))
            return {
                "bid": float(bid),
                "offer": float(offer),
                "mid": mid,
                "timestamp": now,
                "source": source,
            }
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
    while stop_event is None or not stop_event.is_set():
        try:
            await asyncio.to_thread(validate_socket_heartbeat)
            await asyncio.to_thread(evaluate_multi_source_rotation_sweep, cfg=cfg)
            for slot_id, asset_epic in enumerate(engine.active_slots):
                try:
                    tick_data = await engine.get_latest_quote(asset_epic)
                except Exception as exc:
                    _execution_logger.error(
                        "Execution loop exception for %s: %s", asset_epic, exc
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
                        target=PIERCE_LOWER_Z,
                        block_reason=lite_valve_block_status(),
                    )
                except Exception:
                    pass

                if z_score <= PIERCE_LOWER_Z or z_score >= PIERCE_UPPER_Z:
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
        except Exception as exc:
            _execution_logger.error("Execution loop exception encountered: %s", exc)
            log_engine(
                f"ParallelStrategySweep: loop guard {type(exc).__name__}: {exc}"
            )

        try:
            await asyncio.sleep(max(0.1, float(interval_sec)))
        except asyncio.CancelledError:
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
    with _sweep_dispatch_lock:
        if is_api_trading_paused():
            set_last_gate_suppression_reason("api_trading_paused")
            return
        direction = "BUY" if float(z_score) <= PIERCE_LOWER_Z else "SELL"
        try:
            from runtime.trade_manager import dispatch_piercing_zone_order

            dispatch_piercing_zone_order(epic, direction, z_score=z_score, cfg=cfg)
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


def start_stacked_dual_asset_tracks() -> None:
    global _stacked_thread, _execution_focus_target
    stop_cognitive_cascade()
    _execution_focus_target = PRIMARY_STACKED_EPIC
    if _stacked_thread is not None and _stacked_thread.is_alive():
        return
    _stacked_stop.clear()
    cfg = None
    try:
        from system.config_loader import ConfigLoader

        cfg = ConfigLoader().load()
    except Exception:
        pass
    poll_sec = float(ROTATION_SWEEP_SEC)
    cfg_ref = cfg

    async def _coro() -> None:
        await _stacked_dual_async_loop(cfg=cfg_ref, interval_sec=poll_sec)

    bootstrap_multi_source_rotation_stack(cfg=cfg_ref)
    _stacked_thread = _run_async_poll_loop(name="stacked-dual-asset", coro_factory=_coro)
    log_engine(
        f"MultiSourceRotation: 500ms sweep armed universe={len(ROTATION_UNIVERSE)} "
        f"stack={list(get_active_stack_epics())} poll={poll_sec}s"
    )


def start_cognitive_cascade() -> None:
    """Legacy entry — routes to stacked dual-asset architecture."""
    start_stacked_dual_asset_tracks()


def stop_cognitive_cascade() -> None:
    _stacked_stop.set()


def stop_stacked_dual_asset_tracks() -> None:
    stop_cognitive_cascade()


def reset_cognitive_cascade_for_tests() -> None:
    global _execution_focus_target, _focus_tick_velocity, _ml_sovereignty_active
    global _failover_state, _failover_active, _failover_reason, _active_stack_epics
    global _forex_rotation_locked, _stagnant_since_by_epic, _last_rotation_at
    global _last_rotation_reason, _rotation_sweep_count
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


def _z_score_from_widths(widths: deque[float], current: float) -> float:
    if len(widths) < _MIN_SAMPLES:
        return 0.0
    vals = list(widths)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(len(vals) - 1, 1)
    std = math.sqrt(max(var, 1e-12))
    return (current - mean) / std


def ingest_hub_mid(epic: str, mid: float) -> DualCoreSnapshot | None:
    """Feed a live mid — updates volatility Z and dual-core mode (non-blocking)."""
    key = str(epic or "").strip()
    if not key or mid <= 0:
        return None
    try:
        hist = _mid_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
        hist.append(float(mid))
        if len(hist) < _SHORT_WINDOW:
            return None

        recent = list(hist)[-_SHORT_WINDOW:]
        rolling = list(hist)[-Z_ROLLING_WINDOW:]
        upper = max(recent)
        lower = min(recent)
        width = max(upper - lower, 0.0)
        roll_upper = max(rolling) if rolling else upper
        roll_lower = min(rolling) if rolling else lower
        roll_width = max(roll_upper - roll_lower, 0.0)
        widths = _width_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
        widths.append(width)
        z = _z_score_from_widths(widths, width)
        live_z = _z_score_from_widths(widths, roll_width) if len(rolling) >= _MIN_SAMPLES else z
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
    """Strict canary clamp — 0.5 Wall St index / 1.0 Gold / 1.0 FX."""
    _ = cfg
    e = str(epic or "").upper()
    if "CFPGOLD" in e or "GOLD" in e:
        return CANARY_GOLD_LOT
    if "DOW" in e:
        return CANARY_INDEX_LOT
    if _is_fx_epic(epic):
        return CANARY_FX_LOT
    return CANARY_INDEX_LOT


def resolve_micro_stop_limit_points(rest_client: Any, epic: str) -> tuple[float, float]:
    """Floor TP/SL — broker stop stretched to max(2.0, minStopOrProfitDistance)."""
    from execution.live_broker_order_router import floor_stop_distance_points
    from runtime.virtual_stop_loss import INTERNAL_RISK_CEILING_PTS, stretch_broker_stop_distance

    tp_pts, sl_pts = get_effective_micro_tp_sl()
    tp = floor_stop_distance_points(rest_client, epic, tp_pts).effective_points
    sl = stretch_broker_stop_distance(rest_client, epic, sl_pts)
    sl = max(INTERNAL_RISK_CEILING_PTS, sl)
    return float(tp), float(sl)


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
    global _last_gate_suppression_reason
    _last_gate_suppression_reason = ""
