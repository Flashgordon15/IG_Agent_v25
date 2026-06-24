"""
V2 Cache Reaper — automated eviction of stale inflight / pending order locks.

Purges ghost 'order confirmation overdue' blocks without manual cache clearing.

Runtime state and fulfillment snapshots live in volatile RAM during live sessions;
disk serialization runs only on shutdown hooks (see flush_volatile_caches_to_disk).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.paths import data_dir, project_root

REAPER_THREAD_NAME = "ig-v2-cache-reaper"
REAPER_INTERVAL_SEC = 10.0
REAPER_TIMEOUT_SEC = 60.0
# v6.1 — combined FIFO cap across all night-matrix epics (7 assets).
RING_GOVERNOR_MAX_SLOTS = 50_000
RING_GOVERNOR_ASSET_COUNT = 7
AUDIT_LOG = project_root() / "src" / "data" / "logs" / "self_healing_audit.log"
AUDIT_LOG_PRODUCTION = data_dir() / "logs" / "self_healing_audit.log"

# Legacy disk paths — written only on shutdown flush.
RUNTIME_STATE_PATH = data_dir() / "state" / "runtime_state.json"
ORDER_INFLIGHT_ALIAS = RUNTIME_STATE_PATH
FULFILLMENT_CACHE_PATH = data_dir() / "state" / "fulfillment_cache.json"
FULFILLMENT_CACHE_ALIAS = FULFILLMENT_CACHE_PATH

# Volatile in-memory mirrors (thread-safe).
_VOLATILE_LOCK = threading.Lock()
_RUNTIME_STATE_RAM: dict[str, Any] = {}
_FULFILLMENT_CACHE_RAM: dict[str, Any] = {}

_worker_ref: V2CacheReaper | None = None
_audit_lock = threading.Lock()
_shutdown_flush_registered = False

# v6.1 FIFO ring-buffer governor — volatile live tick tape (global cap).
_TICK_GOVERNOR_LOCK = threading.Lock()
_TICK_FIFO: deque[dict[str, Any]] = deque(maxlen=RING_GOVERNOR_MAX_SLOTS)
_TICK_GOVERNOR_DROPPED = 0


def govern_live_tick_ingest(
    epic: str,
    *,
    bid: float,
    offer: float,
    mid: float,
    source: str = "",
) -> int:
    """
    FIFO ring governor — append live tick; oldest slot evicted at 50k combined cap.

    Returns current occupied slot count after ingest.
    """
    global _TICK_GOVERNOR_DROPPED
    key = str(epic or "").strip()
    if not key or bid <= 0 or offer <= 0 or mid <= 0:
        return tick_governor_slot_count()
    slot = {
        "epic": key,
        "bid": float(bid),
        "offer": float(offer),
        "mid": float(mid),
        "ts": time.time(),
        "source": str(source or ""),
    }
    with _TICK_GOVERNOR_LOCK:
        at_cap = len(_TICK_FIFO) >= RING_GOVERNOR_MAX_SLOTS
        _TICK_FIFO.append(slot)
        if at_cap:
            _TICK_GOVERNOR_DROPPED += 1
        return len(_TICK_FIFO)


def tick_governor_slot_count() -> int:
    with _TICK_GOVERNOR_LOCK:
        return len(_TICK_FIFO)


def tick_governor_dropped_total() -> int:
    with _TICK_GOVERNOR_LOCK:
        return int(_TICK_GOVERNOR_DROPPED)


def volatile_tick_slots_for_epic(epic: str) -> list[dict[str, Any]]:
    """Return RAM FIFO tick slots for one epic (newest last)."""
    key = str(epic or "").strip()
    if not key:
        return []
    with _TICK_GOVERNOR_LOCK:
        return [dict(row) for row in _TICK_FIFO if str(row.get("epic") or "") == key]


def tick_governor_telemetry() -> dict[str, Any]:
    with _TICK_GOVERNOR_LOCK:
        epic_counts: dict[str, int] = {}
        for row in _TICK_FIFO:
            e = str(row.get("epic") or "")
            epic_counts[e] = epic_counts.get(e, 0) + 1
        return {
            "slots_used": len(_TICK_FIFO),
            "slots_max": RING_GOVERNOR_MAX_SLOTS,
            "assets_tracked": RING_GOVERNOR_ASSET_COUNT,
            "fifo_dropped_total": int(_TICK_GOVERNOR_DROPPED),
            "per_epic_slots": epic_counts,
        }


def enforce_tick_governor_bounds() -> int:
    """Reaper pass — trim if maxlen guard ever bypassed (returns evicted count)."""
    evicted = 0
    with _TICK_GOVERNOR_LOCK:
        while len(_TICK_FIFO) > RING_GOVERNOR_MAX_SLOTS:
            _TICK_FIFO.popleft()
            evicted += 1
            _TICK_GOVERNOR_DROPPED += 1
    return evicted


def reset_tick_governor_for_tests() -> None:
    global _TICK_GOVERNOR_DROPPED
    with _TICK_GOVERNOR_LOCK:
        _TICK_FIFO.clear()
        _TICK_GOVERNOR_DROPPED = 0


def _volatile_mode_active() -> bool:
    """Live agent: RAM-only mutations. Pytest keeps disk path for isolation."""
    return os.environ.get("IG_AGENT_PYTEST") != "1"


def volatile_runtime_state_get() -> dict[str, Any]:
    with _VOLATILE_LOCK:
        return deepcopy(_RUNTIME_STATE_RAM)


def volatile_runtime_state_set(payload: dict[str, Any]) -> None:
    with _VOLATILE_LOCK:
        _RUNTIME_STATE_RAM.clear()
        if isinstance(payload, dict):
            _RUNTIME_STATE_RAM.update(deepcopy(payload))


def volatile_fulfillment_cache_get() -> dict[str, Any]:
    with _VOLATILE_LOCK:
        return deepcopy(_FULFILLMENT_CACHE_RAM)


def volatile_fulfillment_cache_set(payload: dict[str, Any]) -> None:
    with _VOLATILE_LOCK:
        _FULFILLMENT_CACHE_RAM.clear()
        if isinstance(payload, dict):
            _FULFILLMENT_CACHE_RAM.update(deepcopy(payload))


def volatile_runtime_state_merge(patch: dict[str, Any]) -> None:
    """Shallow-merge into volatile runtime RAM (disaster recovery)."""
    with _VOLATILE_LOCK:
        if isinstance(patch, dict):
            _RUNTIME_STATE_RAM.update(deepcopy(patch))


def volatile_fulfillment_cache_merge(patch: dict[str, Any]) -> None:
    """Shallow-merge into volatile fulfillment RAM."""
    with _VOLATILE_LOCK:
        if isinstance(patch, dict):
            _FULFILLMENT_CACHE_RAM.update(deepcopy(patch))


def refresh_fulfillment_cache_from_engine() -> None:
    """Pull latest fulfillment snapshot from the unified in-memory cache."""
    try:
        from system.unified_fulfillment_cache import get_fulfillment_payload

        volatile_fulfillment_cache_set(get_fulfillment_payload())
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_fulfillment_refresh", exc)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".vc_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def flush_volatile_caches_to_disk(
    *,
    runtime_path: Path | None = None,
    fulfillment_path: Path | None = None,
) -> None:
    """Shutdown-only — persist RAM mirrors to disk atomically."""
    rt_path = runtime_path or RUNTIME_STATE_PATH
    ff_path = fulfillment_path or FULFILLMENT_CACHE_PATH
    with _VOLATILE_LOCK:
        runtime_blob = deepcopy(_RUNTIME_STATE_RAM)
        fulfillment_blob = deepcopy(_FULFILLMENT_CACHE_RAM)
    if runtime_blob:
        _atomic_write_json(rt_path, runtime_blob)
    if fulfillment_blob:
        _atomic_write_json(ff_path, fulfillment_blob)


def hydrate_volatile_caches_from_disk() -> None:
    """Boot — seed RAM from last shutdown snapshot if present."""
    for path, setter in (
        (RUNTIME_STATE_PATH, volatile_runtime_state_set),
        (FULFILLMENT_CACHE_PATH, volatile_fulfillment_cache_set),
    ):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                setter(payload)
        except (OSError, json.JSONDecodeError) as exc:
            log_guarded_exception("v2_cache_reaper_hydrate", exc)


def register_volatile_cache_shutdown_flush() -> None:
    global _shutdown_flush_registered
    if _shutdown_flush_registered:
        return
    _shutdown_flush_registered = True
    try:
        import atexit

        atexit.register(_shutdown_volatile_flush)
    except Exception:
        pass


def _shutdown_volatile_flush() -> None:
    if not _volatile_mode_active():
        return
    try:
        from system.runtime_state_persist import _collect_state

        volatile_runtime_state_set(_collect_state())
        refresh_fulfillment_cache_from_engine()
        flush_volatile_caches_to_disk()
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_shutdown_flush", exc)


def reset_volatile_caches_for_tests() -> None:
    with _VOLATILE_LOCK:
        _RUNTIME_STATE_RAM.clear()
        _FULFILLMENT_CACHE_RAM.clear()
    reset_tick_governor_for_tests()


@dataclass(frozen=True)
class StaleCacheHit:
    epic: str
    source: str
    age_sec: float
    deal_reference: str
    order_type: str = ""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_audit(record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str)
    for path in (AUDIT_LOG, AUDIT_LOG_PRODUCTION):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _audit_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            log_guarded_exception("v2_cache_reaper_audit", exc)


def _read_volatile_runtime_snapshot() -> dict[str, Any]:
    """RAM-only runtime mirror — no disk reads on the hot reaper path."""
    snap = volatile_runtime_state_get()
    if snap:
        return snap
    ff = volatile_fulfillment_cache_get()
    if ff:
        return ff
    return {}


def _scan_stale_in_memory(*, timeout_sec: float) -> list[StaleCacheHit]:
    from execution.entry_inflight import get_entry_in_flight, list_entries_for_reaper
    from execution.pending_order_reconcile import get_pending, list_pending_orders

    now = time.time()
    hits: list[StaleCacheHit] = []

    for pending in list_pending_orders():
        age = now - float(pending.local_created_at)
        if age < timeout_sec:
            continue
        hits.append(
            StaleCacheHit(
                epic=pending.epic,
                source="pending_order",
                age_sec=age,
                deal_reference=str(pending.broker_deal_reference or "").strip(),
                order_type=pending.order_type,
            )
        )

    for entry in list_entries_for_reaper():
        age = now - float(entry.local_created_at)
        if age < timeout_sec:
            continue
        hits.append(
            StaleCacheHit(
                epic=entry.epic,
                source="entry_inflight",
                age_sec=age,
                deal_reference=str(entry.broker_deal_reference or "").strip(),
            )
        )

    _ = get_entry_in_flight
    _ = get_pending
    return hits


def _broker_has_position(rest_client: Any, epic: str) -> bool:
    if rest_client is None:
        return False
    try:
        if hasattr(rest_client, "has_open_position"):
            return bool(rest_client.has_open_position(epic))
    except Exception:
        pass
    try:
        for item in rest_client.open_positions():
            market = item.get("market") or {}
            position = item.get("position") or {}
            if str(market.get("epic") or "") != epic:
                continue
            if float(position.get("size", 0) or 0) > 0:
                return True
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_positions", exc)
    return False


def _evict_epic_caches(epic: str, *, reason: str) -> None:
    from execution.entry_inflight import clear_entry
    from execution.pending_order_reconcile import resolve_pending

    resolve_pending(epic, reason=reason)
    clear_entry(epic)
    try:
        from system.runtime_state_persist import request_save

        request_save()
        refresh_fulfillment_cache_from_engine()
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_volatile_sync", exc)


def _unlock_epic_gateway(epic: str) -> None:
    try:
        from system.unified_fulfillment_cache import heal_epic_execution_gateway

        heal_epic_execution_gateway(epic)
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_gateway", exc)


def _async_broker_sync(rest_client: Any, epic: str, on_complete: Any) -> None:
    def _run() -> None:
        try:
            present = _broker_has_position(rest_client, epic)
            on_complete(present)
        except Exception as exc:
            log_guarded_exception("v2_cache_reaper_async", exc)
            on_complete(False)

    threading.Thread(
        target=_run,
        name=f"v2-reaper-sync-{epic[:12]}",
        daemon=True,
    ).start()


class V2CacheReaper:
    """Background worker — evict stale inflight tokens after 60s without deal_id."""

    def __init__(
        self,
        rest_client: Any,
        *,
        config: Any | None = None,
        interval_sec: float = REAPER_INTERVAL_SEC,
        timeout_sec: float = REAPER_TIMEOUT_SEC,
    ) -> None:
        self._rest = rest_client
        self._config = config
        self._interval = max(2.0, float(interval_sec))
        self._timeout = max(30.0, float(timeout_sec))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        self._last_broker_sync: dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=REAPER_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()
        log_engine(
            f"V2CacheReaper started (interval={self._interval:.0f}s "
            f"timeout={self._timeout:.0f}s)"
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 3.0)
        self._thread = None

    def tick_once(self) -> int:
        """Single reaper pass — returns number of epics evicted."""
        _ = _read_volatile_runtime_snapshot()
        enforce_tick_governor_bounds()
        refresh_fulfillment_cache_from_engine()
        hits = _scan_stale_in_memory(timeout_sec=self._timeout)
        evicted = 0
        for hit in hits:
            if self._evict_if_broker_flat(hit):
                evicted += 1
        return evicted

    def _evict_if_broker_flat(self, hit: StaleCacheHit) -> bool:
        epic = hit.epic
        now = time.time()
        with self._sync_lock:
            last = self._last_broker_sync.get(epic, 0.0)
            if now - last < 5.0:
                return False
            self._last_broker_sync[epic] = now

        if _broker_has_position(self._rest, epic):
            return False

        reason = (
            f"V2CacheReaper: {hit.source} timeout anomaly "
            f"({hit.age_sec:.0f}s, no deal_id) — broker flat, caches wiped"
        )
        _evict_epic_caches(epic, reason=reason)
        _unlock_epic_gateway(epic)
        _append_audit(
            {
                "ts": _utc_iso(),
                "event": "cache_eviction",
                "epic": epic,
                "source": hit.source,
                "age_sec": round(hit.age_sec, 1),
                "deal_reference": hit.deal_reference,
                "order_type": hit.order_type,
                "broker_position": False,
                "accepting_ticks": True,
            }
        )
        log_engine(f"SELF_HEAL {reason}")
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick_once()
            except Exception as exc:
                log_guarded_exception("v2_cache_reaper_loop", exc)


def start_v2_cache_reaper(
    rest_client: Any,
    *,
    config: Any | None = None,
    interval_sec: float = REAPER_INTERVAL_SEC,
    timeout_sec: float = REAPER_TIMEOUT_SEC,
) -> V2CacheReaper | None:
    global _worker_ref
    if rest_client is None:
        return None
    try:
        worker = V2CacheReaper(
            rest_client,
            config=config,
            interval_sec=interval_sec,
            timeout_sec=timeout_sec,
        )
        worker.start()
        register_volatile_cache_shutdown_flush()
        try:
            from system.shutdown_cleanup import start_state_synchronization_pipeline

            start_state_synchronization_pipeline()
        except Exception as exc:
            log_guarded_exception("v2_cache_reaper_state_sync", exc)
        try:
            hydrate_volatile_caches_from_disk()
        except Exception as exc:
            log_guarded_exception("v2_cache_reaper_hydrate_boot", exc)
        try:
            worker.tick_once()
        except Exception as exc:
            log_guarded_exception("v2_cache_reaper_boot_tick", exc)
        _worker_ref = worker
        return worker
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_start", exc)
        return None


def stop_v2_cache_reaper(worker: V2CacheReaper | None = None) -> None:
    global _worker_ref
    target = worker if worker is not None else _worker_ref
    if target is None:
        return
    try:
        target.stop()
    except Exception as exc:
        log_guarded_exception("v2_cache_reaper_stop", exc)
    if target is _worker_ref:
        _worker_ref = None


def reset_v2_cache_reaper_for_tests() -> None:
    stop_v2_cache_reaper()
    reset_volatile_caches_for_tests()
