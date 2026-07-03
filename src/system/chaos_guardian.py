"""
Chaos Guardian — 24/7 token-bucket governor, reconnect backoff, state heal, packet hygiene.

Production SRE layer: rate-limit outbound broker calls, heal feed/execution channels,
reconcile local vs exchange state with REST-on-anomaly-only, emergency flatten on drift.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from system.engine_log import log_engine

_RECONCILE_INTERVAL_SEC = 1.0
_RECONCILE_INTERVAL_CONSERVATION_SEC = 10.0
_token_conservation_mode = False
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0
_BACKOFF_JITTER_FRAC = 0.25
_HISTORY_MAX = 64

# Static reconciliation register pool — sized to max concurrent asset target.
_DEFAULT_MAX_CONCURRENT_ASSETS = 32

# Token bucket capacities — aligned with IG REST budget (3/min) and Yahoo polling
# ig_orders: dedicated lane with burst floor so multi-epic signals don't starve execution.
_BUCKET_SPECS: dict[str, tuple[float, float]] = {
    "ig_rest": (3.0, 3.0 / 60.0),
    "ig_orders": (3.0, 3.0 / 60.0),
    # GET /confirms/{dealRef} polls — isolated from POST /positions/otc lane.
    "ig_confirms": (24.0, 24.0 / 60.0),
    "ig_ledger": (2.0, 2.0 / 60.0),
    # Night-matrix poller: 7 epics / ~3s ≈ 2.3 req/s — old 8/min starved feeds.
    "yahoo": (14.0, 7.0 / 3.0),
}
_ORDER_TOKEN_FLOOR = 1.0
_ORDER_STARVE_QUEUED_WAITS = 3
_CRITICAL_BUCKETS = ("ig_orders",)

_lock = threading.RLock()
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": True,
    "token_buckets": {},
    "connections": {},
    "reconnection_history": [],
    "state_sync": {},
    "packet_sanitization": {},
    "emergency_actions": [],
    "ts": 0.0,
}
_reconnect_history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAX)
_emergency_log: deque[dict[str, Any]] = deque(maxlen=32)
_asset_stream_failures: deque[dict[str, Any]] = deque(maxlen=64)
_daemon_thread: threading.Thread | None = None
_daemon_stop = threading.Event()
_rest_client: Any | None = None
_state_sync_discrepancies: list[dict[str, Any]] = []
_reconcile_registers: list[dict[str, Any]] = []
_backup_compliance_log: deque[dict[str, Any]] = deque(maxlen=32)

# --- Iron Ledger: lock-free read model for HTTP/cockpit (writer: trading loop @ 500ms) ---
_IRON_LEDGER_COMMIT_INTERVAL_SEC = 0.5
_iron_ledger_view: tuple[int, str, dict[str, Any]] = (0, "{}", {})


class IronLedgerSnapshot:
    """
    Isolated read-only cache — API threads copy the latest frozen JSON snapshot only.

    Writers atomically swap a versioned tuple; readers never acquire trading locks.
    """

    @staticmethod
    def commit(state: dict[str, Any]) -> int:
        global _iron_ledger_view
        try:
            frozen_str = json.dumps(state, separators=(",", ":"), default=str, sort_keys=True)
            frozen_dict = json.loads(frozen_str)
        except Exception as exc:
            log_engine(f"IronLedger: commit rejected {type(exc).__name__}: {exc}")
            return _iron_ledger_view[0]
        ver = int(_iron_ledger_view[0]) + 1
        _iron_ledger_view = (ver, frozen_str, frozen_dict)
        return ver

    @staticmethod
    def read() -> dict[str, Any]:
        return dict(_iron_ledger_view[2])

    @staticmethod
    def read_section(key: str) -> dict[str, Any]:
        section = _iron_ledger_view[2].get(key)
        return dict(section) if isinstance(section, dict) else {}

    @staticmethod
    def version() -> int:
        return int(_iron_ledger_view[0])

    @staticmethod
    def json() -> str:
        return str(_iron_ledger_view[1])

    @staticmethod
    def commit_interval_sec() -> float:
        return _IRON_LEDGER_COMMIT_INTERVAL_SEC


def commit_iron_ledger_snapshot(state: dict[str, Any]) -> int:
    """Atomic publish from the master trading loop (single writer)."""
    return IronLedgerSnapshot.commit(state)


def read_iron_ledger_snapshot() -> dict[str, Any]:
    """Lock-free full ledger read for diagnostics."""
    return IronLedgerSnapshot.read()


def read_iron_ledger_orchestrator() -> dict[str, Any]:
    return IronLedgerSnapshot.read_section("orchestrator")


def read_iron_ledger_guardian() -> dict[str, Any]:
    return IronLedgerSnapshot.read_section("guardian")


def seed_iron_ledger_for_tests(
    *,
    orchestrator: dict[str, Any] | None = None,
    guardian: dict[str, Any] | None = None,
) -> int:
    now = time.time()
    return IronLedgerSnapshot.commit(
        {
            "ts": now,
            "orchestrator": orchestrator
            or {
                "ok": True,
                "healthy": True,
                "primed": True,
                "warming_up": False,
                "scoreboard": {
                    "total_pp": 1000,
                    "rank": "standard",
                    "capacity_multiplier": 1.0,
                    "size_factor_multiplier": 1.0,
                },
                "strategy_matrix": {},
                "warmup_logs": [],
                "optimization": {},
                "position_tree": [],
                "last_ring_buffer_refresh_ts": 0.0,
                "stage_status": {},
                "stage_tokens": {},
                "ts": now,
            },
            "guardian": guardian
            or {
                "ok": True,
                "healthy": True,
                "ts": now,
                "reconciliation_registers": {"allocated": 8, "capacity": 8, "registers": []},
                "reconnection_history": [],
                "state_sync_discrepancies": [],
                "token_buckets": {"ig_rest": {}, "ig_orders": {}, "yahoo": {}},
            },
            "platform_pp": 1000,
            "token_buckets": {},
            "position_tree": [],
        }
    )


def _resolve_max_concurrent_assets() -> int:
    """Deterministic slot count from exploration engine or night matrix fallback."""
    try:
        from runtime.portfolio_exploration_engine import compute_max_concurrent_trades
        from system.market_data_hub import NIGHT_MATRIX_EPICS

        n = int(
            compute_max_concurrent_trades(
                available_margin_gbp=10_000.0,
            )
        )
        if n > 0:
            return max(n, len(NIGHT_MATRIX_EPICS))
    except Exception:
        pass
    try:
        from system.market_data_hub import NIGHT_MATRIX_EPICS

        return max(len(NIGHT_MATRIX_EPICS) * 4, 8)
    except Exception:
        return _DEFAULT_MAX_CONCURRENT_ASSETS


def preallocate_reconciliation_registers(max_assets: int | None = None) -> int:
    """Local-first static register pool — pre-sized at boot (STAGE_2)."""
    global _reconcile_registers
    capacity = int(max_assets or _resolve_max_concurrent_assets())
    capacity = max(8, min(capacity, _DEFAULT_MAX_CONCURRENT_ASSETS))
    with _lock:
        _reconcile_registers = [
            {
                "slot": i,
                "epic": "",
                "deal_id": "",
                "sync_state": "idle",
                "anomaly_flags": [],
                "last_tick_ts": 0.0,
            }
            for i in range(capacity)
        ]
    return capacity


def get_reconciliation_register_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "allocated": len(_reconcile_registers),
            "capacity": len(_reconcile_registers),
            "registers": list(_reconcile_registers)[:8],
        }


def wake_guardian_for_boot(*, rest: Any | None = None, max_assets: int | None = None) -> dict[str, Any]:
    """STAGE_2 entry — pre-allocate registers, refresh snapshot, start daemon if needed."""
    allocated = preallocate_reconciliation_registers(max_assets)
    try:
        from system.alert_reporting_matrix import ensure_alert_reporting_boot_ready

        ensure_alert_reporting_boot_ready()
    except Exception:
        pass
    start_chaos_guardian(rest=rest)
    _refresh_snapshot()
    return {
        "ok": True,
        "registers_allocated": allocated,
        "healthy": guardian_healthy(),
        "ts": time.time(),
    }


class TokenBucket:
    """Thread-safe token bucket — O(1) acquire with optional blocking wait."""

    __slots__ = ("name", "capacity", "refill_rate", "tokens", "last_refill", "_lock", "queued_waits")

    def __init__(self, name: str, capacity: float, refill_rate: float) -> None:
        self.name = name
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()
        self.queued_waits = 0

    def _refill_unlocked(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def try_acquire(self, cost: float = 1.0) -> bool:
        with self._lock:
            self._refill_unlocked()
            if self.tokens >= cost:
                self.tokens -= cost
                if self.queued_waits > 0:
                    if self.tokens >= 1.0:
                        self.queued_waits = 0
                    else:
                        self.queued_waits = max(0, self.queued_waits - 1)
                return True
            return False

    def acquire(self, cost: float = 1.0, *, max_wait_sec: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, max_wait_sec)
        while True:
            if self.try_acquire(cost):
                return True
            if time.monotonic() >= deadline:
                with self._lock:
                    self.queued_waits += 1
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refill_unlocked()
            return {
                "name": self.name,
                "capacity": self.capacity,
                "refill_rate_per_sec": round(self.refill_rate, 6),
                "tokens_available": round(self.tokens, 4),
                "queued_waits": self.queued_waits,
            }


_buckets: dict[str, TokenBucket] = {
    name: TokenBucket(name, cap, rate) for name, (cap, rate) in _BUCKET_SPECS.items()
}
_fast_pass_lock = threading.Lock()
_fast_pass_queue: deque[dict[str, Any]] = deque(maxlen=64)
_portfolio_synthesis_lock = threading.Lock()
_portfolio_covariance_compression = 1.0
_portfolio_synthesis_guard: dict[str, Any] = {
    "ok": True,
    "compression_factor": 1.0,
    "order_token_scale": 1.0,
    "ts": 0.0,
}


def sync_portfolio_covariance_compression(factor: float) -> None:
    """Apply portfolio-wide sizing compression to guardian token economics."""
    global _portfolio_covariance_compression, _portfolio_synthesis_guard
    f = float(max(0.1, min(1.0, factor)))
    with _portfolio_synthesis_lock:
        _portfolio_covariance_compression = f
        _portfolio_synthesis_guard = {
            "ok": True,
            "compression_factor": round(f, 4),
            "order_token_scale": round(max(0.5, f), 4),
            "risk_parity_engaged": f < 0.99,
            "ts": time.time(),
        }
    if f < 0.99:
        log_engine(f"ChaosGuardian: portfolio covariance compression={f:.3f}")


def get_portfolio_synthesis_guard_snapshot() -> dict[str, Any]:
    with _portfolio_synthesis_lock:
        return dict(_portfolio_synthesis_guard)


def reset_portfolio_synthesis_guard_for_tests() -> None:
    global _portfolio_covariance_compression, _portfolio_synthesis_guard
    with _portfolio_synthesis_lock:
        _portfolio_covariance_compression = 1.0
        _portfolio_synthesis_guard = {
            "ok": True,
            "compression_factor": 1.0,
            "order_token_scale": 1.0,
            "ts": 0.0,
        }


def record_database_backup_compliance(result: dict[str, Any]) -> None:
    """Append backup archive compliance line — merged into Iron Ledger on next publish."""
    row = {
        "ts": time.time(),
        "ok": bool(result.get("ok")),
        "archive": str(result.get("archive_name") or result.get("archive") or ""),
        "size_bytes": int(result.get("size_bytes") or 0),
        "elapsed_ms": float(result.get("elapsed_ms") or 0),
        "triage_included": bool(result.get("triage_included")),
        "overlay_included": bool(result.get("overlay_included")),
    }
    with _lock:
        _backup_compliance_log.append(row)
    try:
        _sync_guardian_ledger()
    except Exception:
        pass


def reset_backup_compliance_for_tests() -> None:
    with _lock:
        _backup_compliance_log.clear()


def enqueue_fast_pass_token(
    *,
    epic: str,
    direction: str = "",
    score: float = 0.0,
    reason: str = "micro_scalp",
) -> None:
    """Head-of-line priority token for high-velocity predictive microkernel entries."""
    with _fast_pass_lock:
        _fast_pass_queue.appendleft(
            {
                "epic": str(epic or "").strip(),
                "direction": str(direction or "").upper(),
                "score": float(score),
                "reason": str(reason),
                "ts": time.time(),
            }
        )


def get_fast_pass_queue_snapshot() -> list[dict[str, Any]]:
    with _fast_pass_lock:
        return list(_fast_pass_queue)[:16]


def _try_grant_fast_pass(*, bucket_name: str, epic: str = "", cost: float = 1.0) -> bool:
    with _fast_pass_lock:
        if not _fast_pass_queue:
            return False
        entry: dict[str, Any] | None = None
        key = str(epic or "").strip()
        if key:
            for idx, row in enumerate(_fast_pass_queue):
                if row.get("epic") == key:
                    entry = _fast_pass_queue[idx]
                    del _fast_pass_queue[idx]
                    break
        if entry is None:
            entry = _fast_pass_queue.popleft()
    bucket = _buckets.get(bucket_name)
    if bucket is None:
        return True
    with bucket._lock:
        bucket._refill_unlocked()
        bucket.tokens = max(float(bucket.tokens), float(cost))
        bucket.tokens -= float(cost)
    log_engine(
        f"ChaosGuardian: fast-pass token granted bucket={bucket_name} "
        f"epic={entry.get('epic')} reason={entry.get('reason')}"
    )
    return True


@dataclass
class _ChannelState:
    name: str
    connected: bool = False
    last_connect_ts: float = 0.0
    last_disconnect_ts: float = 0.0
    attempts: int = 0
    next_retry_ts: float = 0.0
    uptime_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "attempts": self.attempts,
            "uptime_sec": round(self.uptime_sec, 1),
            "next_retry_in_sec": round(max(0.0, self.next_retry_ts - time.time()), 2),
            "last_disconnect_ts": self.last_disconnect_ts,
        }


_channels: dict[str, _ChannelState] = {
    "ig_stream": _ChannelState("ig_stream"),
    "ig_fix": _ChannelState("ig_fix"),
    "yahoo_feed": _ChannelState("yahoo_feed"),
    "finnhub_ws": _ChannelState("finnhub_ws"),
}


def _is_deal_confirm_poll(method: str, path: str) -> bool:
    """GET /confirms/{dealReference} — polled until terminal dealStatus."""
    if str(method or "").upper() != "GET":
        return False
    p = str(path or "").upper().strip("/")
    return p.startswith("CONFIRMS/") or p == "CONFIRMS"


def _is_order_transmit_path(method: str, path: str) -> bool:
    if str(method or "").upper() not in ("POST", "PUT", "DELETE"):
        return False
    p = str(path or "").upper()
    return any(k in p for k in ("POSITION", "WORKINGORDER", "DEAL", "CONFIRM", "ORDER"))


def _demo_chaos_guardian_order_bypass() -> bool:
    """Demo throughput — skip ig_orders token bucket when traffic governor is bypassed."""
    try:
        from system.demo_execution_plane import demo_throughput_active

        if not demo_throughput_active():
            return False
        from system.config_loader import get_config

        block = get_config().get("demo_throughput_mode") or {}
        return bool(block.get("bypass_traffic_governor", True))
    except Exception:
        return False


_demo_buckets_applied = False
_CONFIRM_POLL_TOKEN_COST = 0.25


def _apply_demo_throughput_bucket_rates() -> None:
    """Raise internal order-lane caps during demo soak (still below IG hard limits)."""
    global _demo_buckets_applied
    if _demo_buckets_applied or not _demo_chaos_guardian_order_bypass():
        return
    _demo_buckets_applied = True
    specs = {
        "ig_orders": (8.0, 8.0 / 60.0),
        "ig_confirms": (48.0, 48.0 / 60.0),
        "ig_ledger": (4.0, 4.0 / 60.0),
    }
    for name, (cap, rate) in specs.items():
        bucket = _buckets.get(name)
        if bucket is None:
            continue
        with bucket._lock:
            bucket.capacity = cap
            bucket.refill_rate = rate
            bucket.tokens = min(bucket.tokens, cap)


def _bucket_for_ig_path(method: str, path: str) -> str:
    if _is_deal_confirm_poll(method, path):
        return "ig_confirms"
    p = str(path or "").upper()
    m = str(method or "").upper()
    if m in ("POST", "PUT", "DELETE") and any(
        k in p for k in ("POSITION", "WORKINGORDER", "DEAL", "CONFIRM", "ORDER")
    ):
        return "ig_orders"
    if any(k in p for k in ("ACCOUNT", "BALANCE", "TRANSACTION", "ACTIVITY", "HISTORY")):
        return "ig_ledger"
    if "POSITION" in p:
        return "ig_ledger"
    return "ig_rest"


def acquire_outbound_token(
    provider: str,
    *,
    method: str = "GET",
    path: str = "",
    category: str = "",
    max_wait_sec: float = 30.0,
    cost: float = 1.0,
    priority: str = "",
    epic: str = "",
) -> bool:
    """
    Acquire token before outbound call. Returns False if exhausted after wait.

    priority='fast_pass' consumes head-of-line micro-scalp queue for instant acquisition.
    """
    token_cost = float(cost)
    if _is_deal_confirm_poll(method, path):
        if _demo_chaos_guardian_order_bypass():
            return True
        bucket_name = "ig_confirms"
        token_cost = _CONFIRM_POLL_TOKEN_COST
    else:
        prov = str(provider or "ig").lower()
        if prov == "yahoo":
            bucket_name = "yahoo"
        elif category in ("orders", "order"):
            bucket_name = "ig_orders"
        elif category in ("ledger", "account", "positions"):
            if _is_order_transmit_path(method, path):
                bucket_name = "ig_orders"
            else:
                bucket_name = "ig_ledger"
        else:
            bucket_name = _bucket_for_ig_path(method, path)

    if str(priority or "").lower() in ("fast_pass", "micro_scalp"):
        if _try_grant_fast_pass(bucket_name=bucket_name, epic=epic, cost=token_cost):
            return True

    _apply_demo_throughput_bucket_rates()

    bucket = _buckets.get(bucket_name)
    if bucket is None:
        return True
    ok = bucket.acquire(token_cost, max_wait_sec=max_wait_sec)
    if not ok:
        log_engine(
            f"ChaosGuardian: token bucket exhausted bucket={bucket_name} "
            f"path={method} {path[:48]}"
        )
        if bucket_name == "ig_orders":
            replenish_critical_buckets()
    return ok


def replenish_critical_buckets() -> dict[str, Any]:
    """
    Restore minimum order-lane headroom when queued waits block execution.

    Surgical refill — does not touch ig_rest/yahoo; clears phantom queued_waits
    after restoring one order token so the next dispatch can proceed.
    """
    out: dict[str, Any] = {}
    for name in _CRITICAL_BUCKETS:
        bucket = _buckets.get(name)
        if bucket is None:
            continue
        with bucket._lock:
            bucket._refill_unlocked()
            starved = bucket.queued_waits >= _ORDER_STARVE_QUEUED_WAITS or (
                bucket.queued_waits > 0 and bucket.tokens < _ORDER_TOKEN_FLOOR
            )
            if not starved:
                continue
            if bucket.tokens < _ORDER_TOKEN_FLOOR:
                bucket.tokens = _ORDER_TOKEN_FLOOR
            bucket.queued_waits = 0
            out[name] = {
                "replenished": True,
                "tokens_available": round(bucket.tokens, 4),
                "queued_waits": 0,
            }
    if out:
        log_engine(f"ChaosGuardian: critical bucket replenish {out}")
    return out


def _decay_stale_token_queue_waits() -> None:
    """Clear stale queued_waits when buckets have headroom (no active starvation)."""
    for bucket in _buckets.values():
        with bucket._lock:
            bucket._refill_unlocked()
            if bucket.queued_waits <= 0:
                continue
            if bucket.tokens >= 1.0:
                bucket.queued_waits = 0
            elif bucket.tokens >= 0.5:
                bucket.queued_waits = max(0, bucket.queued_waits - 1)


def get_token_bucket_snapshots() -> dict[str, Any]:
    return {name: b.snapshot() for name, b in _buckets.items()}


def engage_token_conservation_mode(*, reason: str = "") -> dict[str, Any]:
    """Relax reconcile cadence to 10s when REST budget is rate-limited."""
    global _token_conservation_mode
    _token_conservation_mode = True
    log_engine(
        f"ChaosGuardian: Token Conservation Mode engaged "
        f"reconcile={_RECONCILE_INTERVAL_CONSERVATION_SEC:.0f}s reason={reason[:80]}"
    )
    _refresh_snapshot()
    return {
        "ok": True,
        "token_conservation_mode": True,
        "reconcile_interval_sec": _RECONCILE_INTERVAL_CONSERVATION_SEC,
        "reason": reason[:120],
    }


def release_token_conservation_mode() -> dict[str, Any]:
    """Restore default 1s reconcile cadence after rate-limit recovery."""
    global _token_conservation_mode
    _token_conservation_mode = False
    log_engine("ChaosGuardian: Token Conservation Mode released")
    _refresh_snapshot()
    return {
        "ok": True,
        "token_conservation_mode": False,
        "reconcile_interval_sec": _RECONCILE_INTERVAL_SEC,
    }


def token_conservation_active() -> bool:
    return bool(_token_conservation_mode)


def current_reconcile_interval_sec() -> float:
    if _token_conservation_mode:
        return _RECONCILE_INTERVAL_CONSERVATION_SEC
    return _RECONCILE_INTERVAL_SEC


def clear_token_queue_delays(*, refill: bool = True) -> dict[str, Any]:
    """Reset queued_waits on all token buckets; optionally refill to capacity."""
    out: dict[str, Any] = {}
    for name, bucket in _buckets.items():
        with bucket._lock:
            bucket.queued_waits = 0
            if refill:
                bucket.tokens = bucket.capacity
                bucket.last_refill = time.monotonic()
            out[name] = {
                "queued_waits": bucket.queued_waits,
                "tokens_available": round(bucket.tokens, 4),
            }
    return out


def compute_reconnect_delay(attempt: int) -> float:
    """Exponential backoff with jitter — 1s, 2s, 4s… capped at 30s."""
    attempt = max(0, int(attempt))
    base = min(_BACKOFF_MAX_SEC, _BACKOFF_BASE_SEC * (2**attempt))
    jitter = base * _BACKOFF_JITTER_FRAC * random.random()
    return min(_BACKOFF_MAX_SEC, base + jitter)


def register_channel(name: str) -> None:
    key = str(name or "").strip()
    if not key:
        return
    with _lock:
        if key not in _channels:
            _channels[key] = _ChannelState(key)


def notify_channel_connected(name: str) -> None:
    register_channel(name)
    now = time.time()
    with _lock:
        ch = _channels[name]
        if ch.last_connect_ts > 0 and ch.connected:
            ch.uptime_sec += now - ch.last_connect_ts
        ch.connected = True
        ch.last_connect_ts = now
        ch.attempts = 0
        ch.next_retry_ts = 0.0
        _reconnect_history.append(
            {"ts": now, "channel": name, "event": "connected", "attempt": 0}
        )


def notify_channel_disconnected(name: str, *, reason: str = "") -> float:
    """Schedule reconnect — returns seconds until next retry."""
    register_channel(name)
    now = time.time()
    with _lock:
        ch = _channels[name]
        ch.connected = False
        ch.last_disconnect_ts = now
        delay = compute_reconnect_delay(ch.attempts)
        ch.attempts += 1
        ch.next_retry_ts = now + delay
        row = {
            "ts": now,
            "channel": name,
            "event": "disconnected",
            "reason": str(reason or ""),
            "attempt": ch.attempts,
            "retry_delay_sec": round(delay, 2),
        }
        _reconnect_history.append(row)
    log_engine(
        f"ChaosGuardian: channel disconnect {name} retry_in={delay:.1f}s "
        f"attempt={ch.attempts} reason={reason}"
    )
    try:
        _sync_guardian_ledger()
    except Exception:
        pass
    return delay


def should_delay_reconnect(name: str) -> tuple[bool, float]:
    """True while inside backoff window for channel."""
    with _lock:
        ch = _channels.get(str(name or ""))
        if ch is None or ch.connected:
            return False, 0.0
        wait = ch.next_retry_ts - time.time()
        if wait > 0:
            return True, wait
    return False, 0.0


def _local_position_tree() -> list[dict[str, Any]]:
    tree: list[dict[str, Any]] = []
    try:
        from runtime.portfolio_exploration_engine import get_exploration_state_snapshot

        for row in get_exploration_state_snapshot().get("position_tree") or []:
            if isinstance(row, dict) and row.get("epic"):
                tree.append(dict(row))
    except Exception:
        pass
    if tree:
        return tree
    try:
        from runtime.trade_lifecycle import snapshot as lc_snap

        for deal_id, trade in (lc_snap().get("active") or {}).items():
            if isinstance(trade, dict) and trade.get("epic"):
                tree.append(
                    {
                        "deal_id": str(deal_id),
                        "epic": trade.get("epic"),
                        "direction": trade.get("direction"),
                        "size": trade.get("size"),
                    }
                )
    except Exception:
        pass
    return tree


def _local_anomaly_flags() -> list[str]:
    flags: list[str] = []
    try:
        from system.broker_reconciliation_daemon import get_reconciliation_snapshot

        rec = get_reconciliation_snapshot()
        drift = int(rec.get("drift_count") or 0)
        if drift > 1:
            flags.append(f"reconcile_cache_drift:{drift}")
    except Exception:
        pass

    local_n = len(_local_position_tree())
    try:
        from runtime.active_lifecycle_trades import list_active_lifecycle_trades
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        store = LearningStore(str(get_config().learning_db))
        lc_n = len(list_active_lifecycle_trades(store))
        if abs(local_n - lc_n) > 1 and max(local_n, lc_n) > 0:
            flags.append(f"lifecycle_tree_mismatch:tree={local_n}_lc={lc_n}")
    except Exception:
        pass

    try:
        from execution.pending_order_reconcile import is_unresolved_overdue, list_pending_orders

        for pending in list_pending_orders():
            if is_unresolved_overdue(pending.epic):
                flags.append(f"order_timeout:{pending.epic}")
                break
    except Exception:
        pass

    return flags


def _emergency_flatten_drift(*, rest: Any, reason: str) -> dict[str, Any]:
    """Bypass strategy layers — explicit broker flatten for verified drift."""
    result: dict[str, Any] = {"ok": False, "closed": [], "errors": [], "reason": reason}
    if rest is None:
        result["errors"].append("no_rest_client")
        return result
    if not acquire_outbound_token("ig", category="orders", max_wait_sec=15.0):
        result["errors"].append("token_bucket_exhausted")
        return result
    try:
        positions = rest.open_positions() if hasattr(rest, "open_positions") else []
        if not isinstance(positions, list):
            positions = list(positions or [])
    except Exception as exc:
        result["errors"].append(f"open_positions:{type(exc).__name__}")
        return result

    try:
        from system.config_loader import get_config

        cfg = get_config()
        currency = str(getattr(cfg, "currency_code", None) or "GBP")
    except Exception:
        currency = "GBP"

    for item in positions:
        pos = (item or {}).get("position") or item or {}
        mkt = (item or {}).get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("deal_id") or "")
        epic = str(mkt.get("epic") or pos.get("epic") or "")
        side = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        if not deal_id or size <= 0:
            continue
        close_dir = "SELL" if side == "BUY" else "BUY"
        try:
            if not acquire_outbound_token("ig", category="orders", max_wait_sec=10.0):
                result["errors"].append(f"token_exhausted:{deal_id}")
                continue
            rest.close_position(
                deal_id,
                direction=close_dir,
                size=size,
                epic=epic or None,
                currency_code=currency,
            )
            result["closed"].append(deal_id)
            log_engine(f"ChaosGuardian: emergency flatten closed {epic} deal={deal_id}")
        except Exception as exc:
            result["errors"].append(f"{deal_id}:{type(exc).__name__}")
    result["ok"] = len(result["closed"]) > 0 or len(positions) == 0
    row = {"ts": time.time(), "action": "emergency_flatten", **result}
    _emergency_log.append(row)
    try:
        from system.alert_reporting_matrix import notify_emergency_flatten

        notify_emergency_flatten(
            reason=str(reason),
            closed=len(result.get("closed") or []),
            errors=len(result.get("errors") or []),
        )
    except Exception:
        pass
    try:
        from runtime.strategy_kill_switch import trip_master_strategy_kill_switch

        trip_master_strategy_kill_switch(deal_id="", reason=f"guardian_flatten:{reason}")
    except Exception:
        pass
    return result


def run_state_reconcile_tick(*, rest: Any | None = None) -> dict[str, Any]:
    """Local-first reconcile — REST only on anomaly flag."""
    global _state_sync_discrepancies
    client = rest if rest is not None else _rest_client
    flags = _local_anomaly_flags()
    sync: dict[str, Any] = {
        "local_anomalies": flags,
        "broker_queried": False,
        "drift_verified": False,
        "orphans": [],
        "healthy": len(flags) == 0,
    }

    if not flags:
        with _lock:
            _state_sync_discrepancies = []
        return sync

    sync["broker_queried"] = True
    if not acquire_outbound_token("ig", category="ledger", max_wait_sec=5.0):
        sync["healthy"] = False
        sync["error"] = "ledger_token_exhausted"
        return sync

    try:
        from system.broker_reconciliation_daemon import run_reconciliation_once

        rec = run_reconciliation_once(rest=client)
        sync.update(
            {
                "broker_positions": rec.get("broker_positions"),
                "internal_positions": rec.get("internal_positions"),
                "drift_count": rec.get("drift_count"),
            }
        )
        drift = int(rec.get("drift_count") or 0)
        sync["drift_verified"] = drift > 1
        if sync["drift_verified"]:
            disc = {
                "ts": time.time(),
                "type": "position_drift",
                "broker": rec.get("broker_positions"),
                "internal": rec.get("internal_positions"),
                "reason": rec.get("last_drift_reason"),
            }
            _state_sync_discrepancies.append(disc)
            if client is not None:
                flatten = _emergency_flatten_drift(
                    rest=client,
                    reason=str(rec.get("last_drift_reason") or "drift"),
                )
                sync["emergency_flatten"] = flatten
    except Exception as exc:
        sync["healthy"] = False
        sync["error"] = f"{type(exc).__name__}: {exc}"

    with _lock:
        _snapshot["state_sync"] = dict(sync)
    return sync


def build_guardian_snapshot_body() -> dict[str, Any]:
    """Build guardian telemetry — writer thread only (never on API request path)."""
    try:
        from system.packet_validator import get_packet_sanitizer_health

        packet_health = get_packet_sanitizer_health()
    except Exception:
        packet_health = {}

    _decay_stale_token_queue_waits()

    body = {
        "ok": True,
        "healthy": True,
        "token_conservation_mode": bool(_token_conservation_mode),
        "reconcile_interval_sec": current_reconcile_interval_sec(),
        "token_buckets": get_token_bucket_snapshots(),
        "connections": {k: v.to_dict() for k, v in _channels.items()},
        "reconnection_history": list(_reconnect_history)[-20:],
        "state_sync": dict(_snapshot.get("state_sync") or {}),
        "reconciliation_registers": get_reconciliation_register_snapshot(),
        "state_sync_discrepancies": list(_state_sync_discrepancies)[-10:],
        "packet_sanitization": packet_health,
        "asset_stream_failures": list(_asset_stream_failures)[-15:],
        "emergency_actions": list(_emergency_log)[-5:],
        "portfolio_synthesis": get_portfolio_synthesis_guard_snapshot(),
        "database_backup_compliance": list(_backup_compliance_log)[-10:],
        "iron_ledger_version": IronLedgerSnapshot.version(),
        "ts": time.time(),
    }
    if bool(packet_health.get("circuit_breaker_active")):
        body["healthy"] = False
    return body


def _sync_guardian_ledger(body: dict[str, Any] | None = None) -> int:
    """Merge guardian writer snapshot into the Iron Ledger without blocking readers."""
    payload = dict(body or build_guardian_snapshot_body())
    current = IronLedgerSnapshot.read()
    merged = {
        "ts": time.time(),
        "platform_pp": int(current.get("platform_pp") or 1000),
        "token_buckets": dict(payload.get("token_buckets") or {}),
        "position_tree": list(current.get("position_tree") or []),
        "orchestrator": dict(current.get("orchestrator") or {}),
        "guardian": payload,
    }
    return IronLedgerSnapshot.commit(merged)


def _refresh_snapshot() -> None:
    body = build_guardian_snapshot_body()
    with _lock:
        _snapshot.clear()
        _snapshot.update(body)
    _sync_guardian_ledger(body)


def record_asset_stream_failure(epic: str, reason: str) -> None:
    """Log isolated market stream failure — does not trip global guardian health."""
    row = {"ts": time.time(), "epic": str(epic or ""), "reason": str(reason or "")[:200]}
    _asset_stream_failures.append(row)
    log_engine(f"ChaosGuardian: asset_stream_failure epic={epic} reason={reason[:80]}")


def get_guardian_status_snapshot() -> dict[str, Any]:
    """Read-only API surface — Iron Ledger only (never blocks on trading locks)."""
    ledger = read_iron_ledger_guardian()
    if ledger.get("ts", 0) > 0:
        return dict(ledger)
    return {
        "ok": True,
        "healthy": True,
        "warming": True,
        "iron_ledger": "pending_first_commit",
        "token_buckets": {},
        "connections": {},
        "ts": 0.0,
    }


def guardian_healthy() -> bool:
    snap = get_guardian_status_snapshot()
    return bool(snap.get("healthy", True))


def _daemon_loop() -> None:
    while not _daemon_stop.wait(current_reconcile_interval_sec()):
        try:
            replenish_critical_buckets()
        except Exception:
            pass
        try:
            run_state_reconcile_tick(rest=_rest_client)
        except Exception as exc:
            log_engine(f"ChaosGuardian: reconcile tick {type(exc).__name__}: {exc}")
        try:
            _refresh_snapshot()
        except Exception:
            pass


def start_chaos_guardian(*, rest: Any | None = None) -> None:
    global _daemon_thread, _rest_client
    _rest_client = rest
    if _daemon_thread is not None and _daemon_thread.is_alive():
        return
    _daemon_stop.clear()
    notify_channel_connected("yahoo_feed")
    try:
        _refresh_snapshot()
        run_state_reconcile_tick(rest=rest)
    except Exception:
        pass
    _daemon_thread = threading.Thread(target=_daemon_loop, name="chaos-guardian", daemon=True)
    _daemon_thread.start()
    log_engine("ChaosGuardian: daemon started (1s reconcile + token governor armed)")


def stop_chaos_guardian() -> None:
    _daemon_stop.set()


def reset_chaos_guardian_for_tests() -> None:
    global _daemon_thread, _rest_client, _state_sync_discrepancies, _reconcile_registers
    global _token_conservation_mode, _demo_buckets_applied
    _token_conservation_mode = False
    _demo_buckets_applied = False
    _daemon_stop.set()
    _daemon_thread = None
    _rest_client = None
    _state_sync_discrepancies = []
    _reconcile_registers = []
    _reconnect_history.clear()
    _emergency_log.clear()
    _asset_stream_failures.clear()
    with _fast_pass_lock:
        _fast_pass_queue.clear()
    with _lock:
        for b in _buckets.values():
            b.tokens = b.capacity
            b.queued_waits = 0
            b.last_refill = time.monotonic()
        for ch in _channels.values():
            ch.connected = False
            ch.attempts = 0
            ch.next_retry_ts = 0.0
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "healthy": True,
                "token_buckets": {},
                "connections": {},
                "reconnection_history": [],
                "state_sync": {},
                "packet_sanitization": {},
                "emergency_actions": [],
                "ts": 0.0,
            }
        )
    global _iron_ledger_view
    _iron_ledger_view = (0, "{}", {})
    seed_iron_ledger_for_tests()
