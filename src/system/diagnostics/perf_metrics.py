"""Thread-safe performance counters — written by hot path, read by boot monitor."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_SLOW_TICK_US = 50_000.0
_FLUSH_INTERVAL_SEC = 2.0
_SNAPSHOT_NAME = "perf_metrics.snapshot.json"


@dataclass
class PerfMetricsSnapshot:
    daily_loss_cache_hits: int = 0
    daily_loss_cache_misses: int = 0
    ml_rows_cache_hits: int = 0
    ml_rows_cache_misses: int = 0
    tick_eval_count: int = 0
    tick_eval_slow_count: int = 0
    tick_eval_max_us: float = 0.0
    last_slow_tick: dict[str, Any] = field(default_factory=dict)
    per_gate_max_us: dict[str, float] = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        dl_total = self.daily_loss_cache_hits + self.daily_loss_cache_misses
        ml_total = self.ml_rows_cache_hits + self.ml_rows_cache_misses
        return {
            "daily_loss_cache": {
                "hits": self.daily_loss_cache_hits,
                "misses": self.daily_loss_cache_misses,
                "hit_rate_pct": round(
                    100.0 * self.daily_loss_cache_hits / dl_total, 1
                )
                if dl_total
                else None,
            },
            "ml_rows_cache": {
                "hits": self.ml_rows_cache_hits,
                "misses": self.ml_rows_cache_misses,
                "hit_rate_pct": round(
                    100.0 * self.ml_rows_cache_hits / ml_total, 1
                )
                if ml_total
                else None,
            },
            "tick_gate_eval": {
                "count": self.tick_eval_count,
                "slow_count": self.tick_eval_slow_count,
                "max_us": round(self.tick_eval_max_us, 1),
                "slow_threshold_us": _SLOW_TICK_US,
                "last_slow": self.last_slow_tick,
                "per_gate_max_us": {
                    k: round(v, 1) for k, v in self.per_gate_max_us.items()
                },
            },
            "updated_at": self.updated_at,
        }


_lock = threading.Lock()
_metrics = PerfMetricsSnapshot()
_flush_thread: threading.Thread | None = None
_flush_stop = threading.Event()
_disk_flush_armed = False


def _enabled() -> bool:
    return os.environ.get("IG_AGENT_PERF_MONITOR", "").strip() in (
        "1",
        "true",
        "yes",
    )


def _system_ready() -> bool:
    try:
        from system.system_state import get_system_state

        return bool(get_system_state().snapshot().get("ready"))
    except Exception:
        return False


def _disk_flush_allowed() -> bool:
    """Gate 5+ only — avoid synchronous snapshot I/O during boot gates G1–G4."""
    return _enabled() and _disk_flush_armed and _system_ready()


def _snapshot_path() -> Any:
    from system.paths import logs_dir

    return logs_dir() / _SNAPSHOT_NAME


def start_disk_flush_after_ready() -> None:
    """Arm periodic snapshot writes once SystemState.ready is True (Gate 5)."""
    global _disk_flush_armed, _flush_thread
    if not _enabled():
        return
    _disk_flush_armed = True
    if _flush_thread is not None:
        flush_snapshot()
        return

    def _loop() -> None:
        while not _flush_stop.wait(_FLUSH_INTERVAL_SEC):
            if _disk_flush_allowed():
                flush_snapshot()

    _flush_thread = threading.Thread(
        target=_loop, name="perf-metrics-flush", daemon=True
    )
    _flush_thread.start()
    flush_snapshot()


def flush_snapshot() -> None:
    """Write current counters to disk for the external monitor process."""
    if not _disk_flush_allowed():
        return
    with _lock:
        payload = _metrics.to_dict()
    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def read_snapshot_file() -> dict[str, Any] | None:
    path = _snapshot_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def record_daily_loss_cache(*, hit: bool) -> None:
    if not _enabled():
        return
    with _lock:
        if hit:
            _metrics.daily_loss_cache_hits += 1
        else:
            _metrics.daily_loss_cache_misses += 1
        _metrics.updated_at = time.time()


def record_ml_rows_cache(*, hit: bool) -> None:
    if not _enabled():
        return
    with _lock:
        if hit:
            _metrics.ml_rows_cache_hits += 1
        else:
            _metrics.ml_rows_cache_misses += 1
        _metrics.updated_at = time.time()


def record_tick_gate_evaluation(
    epic: str,
    *,
    total_us: float,
    gate_us: dict[str, float],
) -> None:
    if not _enabled():
        return
    slowest_gate = max(gate_us, key=gate_us.get, default="") if gate_us else ""
    with _lock:
        _metrics.tick_eval_count += 1
        if total_us > _metrics.tick_eval_max_us:
            _metrics.tick_eval_max_us = total_us
        for name, us in gate_us.items():
            prev = _metrics.per_gate_max_us.get(name, 0.0)
            if us > prev:
                _metrics.per_gate_max_us[name] = us
        if total_us >= _SLOW_TICK_US:
            _metrics.tick_eval_slow_count += 1
            _metrics.last_slow_tick = {
                "epic": epic,
                "total_us": round(total_us, 1),
                "total_ms": round(total_us / 1000.0, 2),
                "slowest_gate": slowest_gate,
                "slowest_gate_us": round(gate_us.get(slowest_gate, 0.0), 1),
                "gate_us": {k: round(v, 1) for k, v in gate_us.items()},
                "at": time.time(),
            }
            print(
                f"[PERF WARN] tick gate eval {total_us / 1000.0:.1f}ms epic={epic} "
                f"slowest={slowest_gate} ({gate_us.get(slowest_gate, 0)/1000:.1f}ms)",
                flush=True,
            )
        _metrics.updated_at = time.time()


def get_snapshot() -> dict[str, Any]:
    with _lock:
        return _metrics.to_dict()
