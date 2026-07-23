"""
Live telemetry state cache — zero-latency in-memory writes, atomic JSON flush.

Trading hot paths write to an in-process dict only. A background daemon thread
debounces disk persistence to ``/tmp/ig_agent_live_state.json`` via atomic
``os.replace`` — never blocks on network I/O or WebSocket emission.
"""

from __future__ import annotations

import copy
import json
import os
import resource
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_DEFAULT_LIVE_PATH = Path("/tmp/ig_agent_live_state.json")
_DEFAULT_SHADOW_PATH = Path("/tmp/ig_agent_shadow_state.json")
_SCHEMA_VERSION = "1.0"
_FLUSH_INTERVAL_SEC = 0.05


def _persist_path_for_process() -> Path:
    from system.identity.shared_memory_bridge import resolve_parallel_track_key

    if resolve_parallel_track_key() == "shadow":
        return _DEFAULT_SHADOW_PATH
    if os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1":
        account = os.environ.get("IG_ACCOUNT_ID", "").strip().upper()
        port = os.environ.get("IG_API_PORT", os.environ.get("PORT", "")).strip()
        if account:
            return Path(f"/tmp/ig_agent_live_state_{account}.json")
        if port.isdigit():
            return Path(f"/tmp/ig_agent_live_state_p{port}.json")
    return _DEFAULT_LIVE_PATH


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _default_payload() -> dict[str, Any]:
    from system.identity.shared_memory_bridge import (
        resolve_parallel_track_key,
        track_label_for_key,
    )

    track_key = resolve_parallel_track_key()
    port_raw = os.environ.get("IG_API_PORT", "").strip()
    api_port = int(port_raw) if port_raw.isdigit() else (9199 if track_key == "shadow" else 8080)
    return {
        "schema_version": _SCHEMA_VERSION,
        "track": track_label_for_key(track_key),
        "api_port": api_port,
        "updated_at_utc": _utc_now_iso(),
        "updated_at_epoch": time.time(),
        "trailing_stops": [],
        "ml_optimization": {
            "top_indicators": [],
            "last_review_cycle": 0,
            "risk_scalar": 1.0,
            "vol_threshold_multiplier": 1.0,
            "size_scalar": 1.0,
            "stop_tighten_scalar": 1.0,
            "last_review_outcome": "idle",
        },
        "system_health": {
            "memory_mb": 0.0,
            "tick_latency_ms": 0.0,
            "api_port": 0,
            "port_listening": False,
            "daemon_pid": os.getpid(),
            "last_tick_epic": "",
            "ticks_recorded": 0,
        },
        "hub_quote_source": {},
    }


class LiveStateCache:
    """Thread-safe live state with debounced atomic JSON persistence."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path if path is not None else _persist_path_for_process()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = _default_payload()
        self._dirty = False
        self._stop = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="live-state-cache-flush",
            daemon=True,
        )
        self._flush_thread.start()
        self._atomic_write(force=True)

    def read_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def record_tick(
        self,
        *,
        epic: str,
        bid: float,
        offer: float,
        latency_ms: float,
        trailing_stops: list[dict[str, Any]] | None = None,
    ) -> None:
        """Non-blocking tick telemetry — merges trailing-stop rows when supplied."""
        mid = (float(bid) + float(offer)) * 0.5
        with self._lock:
            health = dict(self._state.get("system_health") or {})
            health["tick_latency_ms"] = round(float(latency_ms), 3)
            health["last_tick_epic"] = str(epic)
            health["ticks_recorded"] = int(health.get("ticks_recorded") or 0) + 1
            health["memory_mb"] = round(_process_rss_mb(), 2)
            port_raw = os.environ.get("IG_API_PORT", "").strip()
            if port_raw.isdigit() and not int(health.get("api_port") or 0):
                health["api_port"] = int(port_raw)
            health["daemon_pid"] = os.getpid()
            self._state["system_health"] = health
            if trailing_stops is not None:
                self._state["trailing_stops"] = list(trailing_stops)
            else:
                stops = list(self._state.get("trailing_stops") or [])
                for row in stops:
                    if str(row.get("epic") or "") != str(epic):
                        continue
                    entry = float(row.get("entry_price") or 0.0)
                    direction = str(row.get("direction") or "BUY").upper()
                    if entry > 0:
                        if direction == "BUY":
                            profit_pct = (mid - entry) / entry
                            distance = float(row.get("trail_distance_pct") or 0.02)
                            floor = mid * (1.0 - distance)
                            prev_floor = float(row.get("trailing_floor") or 0.0)
                            if floor > prev_floor:
                                row["trailing_floor"] = round(floor, 6)
                        else:
                            profit_pct = (entry - mid) / entry
                            distance = float(row.get("trail_distance_pct") or 0.02)
                            floor = mid * (1.0 + distance)
                            prev_floor = float(row.get("trailing_floor") or entry * 2)
                            if floor < prev_floor:
                                row["trailing_floor"] = round(floor, 6)
                        row["current_price"] = round(mid, 6)
                        row["profit_pct"] = round(profit_pct, 6)
                        trigger = float(row.get("win_lock_trigger_pct") or 0.10)
                        row["win_locked"] = profit_pct >= trigger
                self._state["trailing_stops"] = stops
            self._touch_locked()
            self._dirty = True
            snap = copy.deepcopy(self._state)
        self._sync_shared_memory(snap)

    def upsert_trailing_stop(
        self,
        *,
        epic: str,
        direction: str,
        entry_price: float,
        current_price: float,
        trailing_floor: float,
        trail_distance_pct: float = 0.02,
        win_lock_trigger_pct: float = 0.10,
    ) -> None:
        """Register or refresh a trailing-stop row (win-lock at 10% gain by default)."""
        direction_u = str(direction or "BUY").upper()
        entry = float(entry_price)
        current = float(current_price)
        if entry <= 0:
            return
        if direction_u == "BUY":
            profit_pct = (current - entry) / entry
        else:
            profit_pct = (entry - current) / entry
        row = {
            "epic": str(epic),
            "direction": direction_u,
            "entry_price": round(entry, 6),
            "current_price": round(current, 6),
            "trailing_floor": round(float(trailing_floor), 6),
            "trail_distance_pct": round(float(trail_distance_pct), 6),
            "profit_pct": round(profit_pct, 6),
            "win_lock_trigger_pct": round(float(win_lock_trigger_pct), 6),
            "win_locked": profit_pct >= float(win_lock_trigger_pct),
        }
        with self._lock:
            stops = [s for s in list(self._state.get("trailing_stops") or []) if s.get("epic") != epic]
            stops.append(row)
            self._state["trailing_stops"] = stops
            self._touch_locked()
            self._dirty = True
            snap = copy.deepcopy(self._state)
        self._sync_shared_memory(snap)

    def apply_meta_review(self, review: dict[str, Any]) -> None:
        """Merge ML optimization deltas from MetaReviewer pillar loop."""
        with self._lock:
            ml = dict(self._state.get("ml_optimization") or {})
            ml.update(
                {
                    "top_indicators": list(review.get("top_indicators") or []),
                    "last_review_cycle": int(review.get("cycle") or ml.get("last_review_cycle") or 0),
                    "risk_scalar": float(review.get("risk_scalar") or ml.get("risk_scalar") or 1.0),
                    "vol_threshold_multiplier": float(
                        review.get("vol_threshold_multiplier")
                        or ml.get("vol_threshold_multiplier")
                        or 1.0
                    ),
                    "size_scalar": float(review.get("size_scalar") or ml.get("size_scalar") or 1.0),
                    "stop_tighten_scalar": float(
                        review.get("stop_tighten_scalar") or ml.get("stop_tighten_scalar") or 1.0
                    ),
                    "last_review_outcome": str(
                        review.get("outcome") or ml.get("last_review_outcome") or "idle"
                    ),
                }
            )
            self._state["ml_optimization"] = ml
            self._touch_locked()
            self._dirty = True
            snap = copy.deepcopy(self._state)
        self._sync_shared_memory(snap)

    def update_hub_quote_source(
        self,
        *,
        epic: str,
        source: str,
        staleness_seconds: int,
    ) -> None:
        """Synchronized quote provenance block — visible on dashboard + isolated Flight Deck."""
        epic_key = str(epic or "").strip()
        if not epic_key:
            return
        with self._lock:
            block = dict(self._state.get("hub_quote_source") or {})
            block[epic_key] = {
                "source": str(source),
                "staleness_seconds": int(staleness_seconds),
            }
            self._state["hub_quote_source"] = block
            self._touch_locked()
            self._dirty = True
            snap = copy.deepcopy(self._state)
        self._sync_shared_memory(snap)

    def refresh_system_health(
        self,
        *,
        api_port: int | None = None,
        port_listening: bool | None = None,
        daemon_pid: int | None = None,
    ) -> None:
        with self._lock:
            health = dict(self._state.get("system_health") or {})
            health["memory_mb"] = round(_process_rss_mb(), 2)
            if api_port is not None:
                health["api_port"] = int(api_port)
            if port_listening is not None:
                health["port_listening"] = bool(port_listening)
            if daemon_pid is not None:
                health["daemon_pid"] = int(daemon_pid)
            self._state["system_health"] = health
            self._touch_locked()
            self._dirty = True
            snap = copy.deepcopy(self._state)
        self._sync_shared_memory(snap)

    def flush_now(self) -> None:
        self._atomic_write(force=True)

    def shutdown(self) -> None:
        self._stop.set()
        self._atomic_write(force=True)

    def _touch_locked(self) -> None:
        from system.identity.shared_memory_bridge import (
            resolve_parallel_track_key,
            track_label_for_key,
        )

        track_key = resolve_parallel_track_key()
        self._state["track"] = track_label_for_key(track_key)
        port_raw = os.environ.get("IG_API_PORT", "").strip()
        if port_raw.isdigit():
            self._state["api_port"] = int(port_raw)
        self._state["updated_at_utc"] = _utc_now_iso()
        self._state["updated_at_epoch"] = time.time()

    def _flush_loop(self) -> None:
        while not self._stop.wait(timeout=_FLUSH_INTERVAL_SEC):
            try:
                if self._dirty:
                    self._atomic_write()
            except Exception as exc:
                log_guarded_exception("live_state_cache_flush", exc)

    def _sync_shared_memory(self, payload: dict[str, Any]) -> None:
        """Ultra-fast shared RAM publish — never blocks on disk or network."""
        try:
            from system.identity.shared_memory_bridge import (
                get_shared_memory_bridge,
                resolve_parallel_track_key,
            )

            get_shared_memory_bridge(create=True, track=resolve_parallel_track_key()).write_json(payload)
        except Exception as exc:
            log_guarded_exception("live_state_shared_memory", exc)

    def _atomic_write(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            payload = copy.deepcopy(self._state)
            self._dirty = False
        self._sync_shared_memory(payload)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            with self._lock:
                self._dirty = True
            log_guarded_exception("live_state_cache_write", exc)


def _process_rss_mb() -> float:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports bytes in ru_maxrss; Linux reports kilobytes
        if os.uname().sysname == "Darwin":
            return float(usage.ru_maxrss) / (1024.0 * 1024.0)
        return float(usage.ru_maxrss) / 1024.0
    except Exception:
        return 0.0


_cache_singleton: LiveStateCache | None = None
_cache_lock = threading.Lock()


def get_live_state_cache() -> LiveStateCache:
    global _cache_singleton
    with _cache_lock:
        if _cache_singleton is None:
            from system.identity.shared_memory_bridge import (
                get_shared_memory_bridge,
                resolve_parallel_track_key,
            )

            get_shared_memory_bridge(create=True, track=resolve_parallel_track_key())
            _cache_singleton = LiveStateCache()
            log_engine(f"LiveStateCache: armed path={_cache_singleton._path}")
        return _cache_singleton


def reset_live_state_cache() -> None:
    """Tests only — drop singleton and remove persisted file."""
    global _cache_singleton
    with _cache_lock:
        if _cache_singleton is not None:
            _cache_singleton.shutdown()
        _cache_singleton = None
    try:
        from system.identity.shared_memory_bridge import reset_shared_memory_bridge

        reset_shared_memory_bridge(unlink=True, track="live")
        reset_shared_memory_bridge(unlink=True, track="shadow")
    except Exception:
        pass
    try:
        _DEFAULT_LIVE_PATH.unlink(missing_ok=True)
        _DEFAULT_LIVE_PATH.with_suffix(".json.tmp").unlink(missing_ok=True)
        _DEFAULT_SHADOW_PATH.unlink(missing_ok=True)
        _DEFAULT_SHADOW_PATH.with_suffix(".json.tmp").unlink(missing_ok=True)
    except OSError:
        pass


def read_persisted_live_state(path: Path | None = None) -> dict[str, Any]:
    """Read JSON from disk — API cold-start fallback."""
    target = path if path is not None else _persist_path_for_process()
    if not target.is_file():
        return _default_payload()
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else _default_payload()
    except (OSError, json.JSONDecodeError):
        return _default_payload()
