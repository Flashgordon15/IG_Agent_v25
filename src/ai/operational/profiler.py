"""v28 CIAO — Operational Profiler & Inactivity Investigator (§20)."""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from ai.paths import (
    operational_safety_freeze_path,
    profiler_latency_path,
    rca_diagnostics_dir,
    sentinel_diagnostics_path,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    return float(ordered[f] + (ordered[c] - ordered[f]) * (k - f))


@dataclass
class LatencySample:
    ts: float
    probe: str
    duration_ms: float
    epic: str = ""


@dataclass
class SessionActivity:
    epic: str
    session_open_since: float | None = None
    trades_taken: int = 0
    atr_filter_cleared: bool = False
    gate_fail_counts: dict[str, int] = field(default_factory=dict)
    last_gate_block: str = ""


@dataclass
class OperationalProfiler:
    """Rolling latency percentiles + Inactivity Investigator."""

    window_sec: float = 3600.0
    inactivity_window_sec: float = 3600.0
    _samples: dict[str, deque[LatencySample]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _sessions: dict[str, SessionActivity] = field(default_factory=dict)

    def record_probe(
        self,
        probe: str,
        duration_ms: float,
        *,
        epic: str = "",
    ) -> None:
        key = str(probe or "unknown")
        now = time.time()
        sample = LatencySample(
            ts=now, probe=key, duration_ms=float(duration_ms), epic=epic
        )
        bucket = self._samples[key]
        bucket.append(sample)
        self._trim(bucket, now)
        self._append_latency_log(sample)

    @contextmanager
    def probe(self, name: str, *, epic: str = "") -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record_probe(name, (time.perf_counter() - t0) * 1000.0, epic=epic)

    def _trim(self, bucket: deque[LatencySample], now: float) -> None:
        cutoff = now - self.window_sec
        while bucket and bucket[0].ts < cutoff:
            bucket.popleft()

    def _append_latency_log(self, sample: LatencySample) -> None:
        path = profiler_latency_path()
        row = {
            "ts": _utc_now(),
            "probe": sample.probe,
            "duration_ms": round(sample.duration_ms, 3),
            "epic": sample.epic or None,
        }
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def rolling_percentiles(self, probe: str | None = None) -> dict[str, Any]:
        now = time.time()
        probes = [probe] if probe else list(self._samples.keys())
        out: dict[str, Any] = {
            "ts": _utc_now(),
            "window_sec": self.window_sec,
            "probes": {},
        }
        for name in probes:
            bucket = self._samples.get(name)
            if not bucket:
                continue
            self._trim(bucket, now)
            values = [s.duration_ms for s in bucket]
            if not values:
                continue
            out["probes"][name] = {
                "n": len(values),
                "p50_ms": round(_percentile(values, 50) or 0, 2),
                "p95_ms": round(_percentile(values, 95) or 0, 2),
                "p99_ms": round(_percentile(values, 99) or 0, 2),
                "mean_ms": round(statistics.mean(values), 2),
            }
        return out

    def update_session_activity(
        self,
        epic: str,
        *,
        session_open: bool,
        trade_executed: bool = False,
        atr_filter_cleared: bool = False,
        gate_failures: dict[str, int] | None = None,
        dominant_gate_block: str = "",
    ) -> None:
        key = str(epic or "unknown")
        act = self._sessions.setdefault(key, SessionActivity(epic=key))
        now = time.time()
        if session_open:
            if act.session_open_since is None:
                act.session_open_since = now
        else:
            act.session_open_since = None
            act.trades_taken = 0
            act.gate_fail_counts.clear()
        if trade_executed:
            act.trades_taken += 1
        act.atr_filter_cleared = atr_filter_cleared
        if gate_failures:
            for g, c in gate_failures.items():
                act.gate_fail_counts[g] = act.gate_fail_counts.get(g, 0) + int(c)
        if dominant_gate_block:
            act.last_gate_block = dominant_gate_block

    def _safety_frozen(self) -> bool:
        path = operational_safety_freeze_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return bool(data.get("operational_safety_freeze"))
        except (OSError, json.JSONDecodeError):
            return False

    def _parse_sentinel_gate_blocks(
        self,
        epic: str,
        *,
        limit: int = 500,
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        path = sentinel_diagnostics_path()
        counts: dict[str, int] = defaultdict(int)
        excerpt: list[dict[str, Any]] = []
        if not path.exists():
            return counts, excerpt
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()[-limit:]
        except OSError:
            return counts, excerpt
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("epic") and row.get("epic") != epic:
                continue
            if row.get("unhealthy"):
                counts["sentinel_unhealthy"] += 1
            if row.get("loop_error"):
                counts["loop_error"] += 1
            if row.get("stream_disconnected"):
                counts["stream_disconnected"] += 1
            if row.get("quote_stale"):
                counts["quote_stale"] += 1
            if len(excerpt) < 20:
                excerpt.append(row)
        return counts, excerpt

    def investigate_inactivity(self, epic: str) -> dict[str, Any] | None:
        """Inactivity Investigator — write RCA_DIAGNOSTIC when laws trigger (§20.4)."""
        act = self._sessions.get(epic)
        if act is None or act.session_open_since is None:
            return None
        elapsed = time.time() - act.session_open_since
        if elapsed < self.inactivity_window_sec:
            return None
        if act.trades_taken > 0:
            return None
        if not act.atr_filter_cleared:
            return None
        if self._safety_frozen():
            return None

        sentinel_counts, sentinel_excerpt = self._parse_sentinel_gate_blocks(epic)
        gate_fail_counts = dict(act.gate_fail_counts)
        for k, v in sentinel_counts.items():
            gate_fail_counts[k] = gate_fail_counts.get(k, 0) + v

        dominant = act.last_gate_block
        if gate_fail_counts:
            dominant = max(gate_fail_counts.items(), key=lambda kv: kv[1])[0]

        start = datetime.fromtimestamp(act.session_open_since, tz=timezone.utc)
        end = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "type": "RCA_DIAGNOSTIC",
            "ts": _utc_now(),
            "epic": epic,
            "session_window": {
                "start": start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "end": end.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "duration_min": round(elapsed / 60.0, 1),
            },
            "trades_taken": act.trades_taken,
            "atr_filter_cleared": act.atr_filter_cleared,
            "dominant_gate_block": dominant or "unknown",
            "gate_fail_counts": gate_fail_counts,
            "sentinel_excerpt": sentinel_excerpt[:10],
            "recommended_operator_action": (
                "review gate detail — no auto threshold change"
            ),
        }

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_epic = epic.replace(".", "_")
        out_path = rca_diagnostics_dir() / f"rca_{safe_epic}_{ts}.json"
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        act.session_open_since = time.time()
        return {"ok": True, "path": str(out_path), "payload": payload}

    def maybe_investigate_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for epic in list(self._sessions.keys()):
            rca = self.investigate_inactivity(epic)
            if rca:
                results.append(rca)
        return results


_default_profiler: OperationalProfiler | None = None


def get_operational_profiler() -> OperationalProfiler:
    global _default_profiler
    if _default_profiler is None:
        _default_profiler = OperationalProfiler()
    return _default_profiler
