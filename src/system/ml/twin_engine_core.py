"""
Twin-Engine Core — LiveEngine + ShadowEngine with atomic model hot-swap.

LiveEngine serves tick-time inference on the hot path (GIL-friendly reads).
ShadowEngine maintains a rolling 5-day UTC ring buffer, retrains in an isolated
sub-process every 24h of virtual time, and hot-swaps weights when edge vs a
random-walk baseline exceeds 2.5%.
"""

from __future__ import annotations

import math
import multiprocessing as mp
import os
import random
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception

_FIVE_DAYS_SEC = 5 * 24 * 3600.0
_VIRTUAL_DAY_SEC = 24 * 3600.0
_HOTSWAP_EDGE_THRESHOLD = 0.025
_FEATURE_KEYS = ("adjusted_score", "rsi", "atr_ratio")
_RING_MAX = 500_000


class ShadowDataGuardError(ValueError):
    """Fail-closed — datetime-naive or look-ahead rows are rejected."""


def validate_utc_timestamp(
    ts: Any,
    *,
    latest_ts: float | None = None,
) -> float:
    """
    Normalize tick timestamps to UTC epoch seconds.

    Raises ``ShadowDataGuardError`` on naive datetimes or out-of-order (look-ahead) rows.
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ShadowDataGuardError(
                "FAIL-CLOSED DATA GUARD: datetime-naive timestamp rejected"
            )
        epoch = float(ts.timestamp())
    elif isinstance(ts, (int, float)):
        epoch = float(ts)
    else:
        raise ShadowDataGuardError(
            f"FAIL-CLOSED DATA GUARD: invalid timestamp type {type(ts).__name__}"
        )
    if latest_ts is not None and epoch < latest_ts:
        raise ShadowDataGuardError(
            "FAIL-CLOSED DATA GUARD: look-ahead/out-of-order timestamp rejected"
        )
    return epoch


def _utc_epoch() -> float:
    try:
        from simulation.replay_clock import is_replay_active, now

        if is_replay_active():
            return float(now())
    except Exception:
        pass
    return datetime.now(timezone.utc).timestamp()


@dataclass
class TickSample:
    ts_utc: float
    epic: str
    bid: float
    offer: float
    direction: str
    features: dict[str, float]
    mid_return: float = 0.0
    label: int | None = None


@dataclass
class ModelWeights:
    bias: float = 0.0
    coeffs: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(_FEATURE_KEYS, 0.0)
    )
    version: int = 0
    trained_at: float = 0.0

    def score(self, features: dict[str, float]) -> float:
        z = float(self.bias)
        for key in _FEATURE_KEYS:
            z += float(self.coeffs.get(key, 0.0)) * float(features.get(key, 0.0))
        try:
            return 1.0 / (1.0 + math.exp(-z))
        except OverflowError:
            return 0.0 if z < 0 else 1.0


@dataclass
class ShadowTelemetry:
    win_rate_edge: float = 0.0
    precision_drift: float = 0.0
    sortino_variance: float = 0.0
    random_walk_baseline: float = 0.0
    candidate_score: float = 0.0
    live_score: float = 0.0
    hot_swaps: int = 0
    last_swap_at: float = 0.0
    retrains: int = 0


def _label_from_return(ret: float, *, deadband: float = 1e-8) -> int:
    if ret > deadband:
        return 1
    if ret < -deadband:
        return 0
    return 0


def _random_walk_baseline(samples: list[TickSample]) -> float:
    if len(samples) < 20:
        return 0.5
    wins = 0
    total = 0
    for sample in samples:
        if sample.label is None:
            continue
        guess = 1 if random.random() >= 0.5 else 0
        if guess == sample.label:
            wins += 1
        total += 1
    return wins / total if total > 0 else 0.5


def _precision(predictions: list[float], labels: list[int], *, threshold: float = 0.5) -> float:
    tp = fp = 0
    for pred, label in zip(predictions, labels, strict=False):
        pred_bin = 1 if pred >= threshold else 0
        if pred_bin == 1:
            if label == 1:
                tp += 1
            else:
                fp += 1
    denom = tp + fp
    return tp / denom if denom > 0 else 0.0


def _train_weights_worker(payload: dict[str, Any], out_queue: mp.Queue) -> None:
    """Isolated sub-process trainer — no shared GIL with live ticks."""
    samples_raw: list[dict[str, Any]] = list(payload.get("samples") or [])
    samples: list[TickSample] = [
        TickSample(
            ts_utc=float(row["ts_utc"]),
            epic=str(row["epic"]),
            bid=float(row["bid"]),
            offer=float(row["offer"]),
            direction=str(row["direction"]),
            features={k: float(row["features"].get(k, 0.0)) for k in _FEATURE_KEYS},
            mid_return=float(row.get("mid_return") or 0.0),
            label=int(row["label"]) if row.get("label") is not None else None,
        )
        for row in samples_raw
    ]
    labeled = [s for s in samples if s.label is not None]
    if len(labeled) < 32:
        out_queue.put({"ok": False, "reason": "insufficient_labeled_samples"})
        return

    labels = [int(s.label or 0) for s in labeled]
    pos_rate = sum(labels) / len(labels)
    bias = math.log(max(1e-6, pos_rate) / max(1e-6, 1.0 - pos_rate))

    coeffs: dict[str, float] = {}
    for key in _FEATURE_KEYS:
        xs = [float(s.features.get(key, 0.0)) for s in labeled]
        ys = [float(s.label or 0) for s in labeled]
        mean_x = statistics.fmean(xs) if xs else 0.0
        mean_y = statistics.fmean(ys) if ys else 0.0
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
        den = sum((x - mean_x) ** 2 for x in xs) or 1.0
        coeffs[key] = num / den * 0.05

    candidate = ModelWeights(
        bias=bias,
        coeffs=coeffs,
        version=int(payload.get("version") or 0) + 1,
        trained_at=float(payload.get("trained_at") or time.time()),
    )
    preds = [candidate.score(sample.features) for sample in labeled]
    candidate_precision = _precision(preds, labels)
    live_precision = float(payload.get("live_precision") or 0.0)
    precision_drift = candidate_precision - live_precision
    returns = [float(s.mid_return) for s in labeled]
    sortino_var = statistics.pvariance(returns) if len(returns) > 1 else 0.0
    rw = _random_walk_baseline(labeled)
    win_rate_edge = candidate_precision - rw

    out_queue.put(
        {
            "ok": True,
            "weights": {
                "bias": candidate.bias,
                "coeffs": dict(candidate.coeffs),
                "version": candidate.version,
                "trained_at": candidate.trained_at,
            },
            "telemetry": {
                "win_rate_edge": win_rate_edge,
                "precision_drift": precision_drift,
                "sortino_variance": sortino_var,
                "random_walk_baseline": rw,
                "candidate_score": candidate_precision,
                "live_score": live_precision,
            },
        }
    )


class LiveEngine:
    """Hot-path inference engine — weights swapped atomically."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._weights = ModelWeights()

    def score(self, features: dict[str, float]) -> float:
        with self._lock:
            weights = self._weights
        return weights.score(features)

    def weights_snapshot(self) -> ModelWeights:
        with self._lock:
            return ModelWeights(
                bias=self._weights.bias,
                coeffs=dict(self._weights.coeffs),
                version=self._weights.version,
                trained_at=self._weights.trained_at,
            )

    def atomic_swap(self, new_weights: ModelWeights) -> None:
        with self._lock:
            self._weights = new_weights

    def atomic_swap_timed_ns(self, new_weights: ModelWeights) -> int:
        """Apply swap and return elapsed nanoseconds (integrity probes)."""
        t0 = time.perf_counter_ns()
        self.atomic_swap(new_weights)
        return time.perf_counter_ns() - t0

    def partial_fit(
        self,
        feature_gradients: dict[str, float],
        *,
        learning_rate: float = 0.005,
    ) -> dict[str, float]:
        """
        Incremental online weight adjustment — returns exact deltas applied.

        Hot-path safe: single lock acquisition, no I/O.
        """
        deltas: dict[str, float] = {}
        lr = float(learning_rate)
        with self._lock:
            for key in _FEATURE_KEYS:
                grad = float(feature_gradients.get(key) or 0.0)
                if abs(grad) < 1e-12:
                    continue
                delta = lr * grad
                prev = float(self._weights.coeffs.get(key) or 0.0)
                self._weights.coeffs[key] = prev + delta
                deltas[key] = delta
            if deltas:
                self._weights.version += 1
                self._weights.trained_at = time.time()
        return deltas


class ShadowEngine:
    """Rolling 5-day UTC buffer + virtual-time retrain scheduler."""

    def __init__(self, on_retrain: Callable[[list[dict[str, Any]], float, int], None]) -> None:
        self._on_retrain = on_retrain
        self._lock = threading.Lock()
        self._buffer: deque[TickSample] = deque(maxlen=_RING_MAX)
        self._virtual_origin: float | None = None
        self._last_retrain_virtual: float = 0.0
        self._retrain_inflight = False
        self._telemetry = ShadowTelemetry()
        self._live_precision: float = 0.0

    @property
    def telemetry(self) -> ShadowTelemetry:
        with self._lock:
            return ShadowTelemetry(
                win_rate_edge=self._telemetry.win_rate_edge,
                precision_drift=self._telemetry.precision_drift,
                sortino_variance=self._telemetry.sortino_variance,
                random_walk_baseline=self._telemetry.random_walk_baseline,
                candidate_score=self._telemetry.candidate_score,
                live_score=self._telemetry.live_score,
                hot_swaps=self._telemetry.hot_swaps,
                last_swap_at=self._telemetry.last_swap_at,
                retrains=self._telemetry.retrains,
            )

    def append(self, sample: TickSample) -> None:
        latest: float | None = None
        with self._lock:
            if self._buffer:
                latest = self._buffer[-1].ts_utc
        sample.ts_utc = validate_utc_timestamp(sample.ts_utc, latest_ts=latest)

        trigger = False
        snapshot: list[dict[str, Any]] = []
        live_precision = 0.0
        version = 0
        with self._lock:
            if self._buffer:
                prev = self._buffer[-1]
                prev_mid = (prev.bid + prev.offer) * 0.5
                cur_mid = (sample.bid + sample.offer) * 0.5
                if prev_mid > 0:
                    sample.mid_return = (cur_mid - prev_mid) / prev_mid
                    sample.label = _label_from_return(sample.mid_return)
            self._buffer.append(sample)
            self._trim_locked(sample.ts_utc)
            if self._virtual_origin is None:
                self._virtual_origin = sample.ts_utc
            virtual_elapsed = sample.ts_utc - float(self._virtual_origin)
            if (
                virtual_elapsed - self._last_retrain_virtual >= _VIRTUAL_DAY_SEC
                and not self._retrain_inflight
            ):
                self._last_retrain_virtual = virtual_elapsed
                self._retrain_inflight = True
                snapshot = [self._sample_to_dict(s) for s in list(self._buffer)]
                live_precision = self._live_precision
                version = 0
                trigger = True
        if trigger:
            threading.Thread(
                target=self._on_retrain,
                args=(snapshot, live_precision, version),
                name="twin-engine-retrain",
                daemon=True,
            ).start()

    def _trim_locked(self, now_ts: float) -> None:
        cutoff = now_ts - _FIVE_DAYS_SEC
        while self._buffer and self._buffer[0].ts_utc < cutoff:
            self._buffer.popleft()

    @staticmethod
    def _sample_to_dict(sample: TickSample) -> dict[str, Any]:
        return {
            "ts_utc": sample.ts_utc,
            "epic": sample.epic,
            "bid": sample.bid,
            "offer": sample.offer,
            "direction": sample.direction,
            "features": dict(sample.features),
            "mid_return": sample.mid_return,
            "label": sample.label,
        }


class TwinEngineCore:
    """Coordinator — feeds shadow buffer, serves live scores, atomic hot-swap."""

    def __init__(self) -> None:
        self.live = LiveEngine()
        self.shadow = ShadowEngine(on_retrain=self._run_retrain_subprocess)
        self._pending_swap: tuple[ModelWeights, dict[str, Any]] | None = None
        self._swap_lock = threading.Lock()

    def ingest_and_score(
        self,
        *,
        epic: str,
        ts_utc: float | None,
        bid: float,
        offer: float,
        features: dict[str, float],
        direction: str,
    ) -> float:
        raw_ts = ts_utc if ts_utc is not None else _utc_epoch()
        with self.shadow._lock:
            latest = self.shadow._buffer[-1].ts_utc if self.shadow._buffer else None
        epoch = validate_utc_timestamp(raw_ts, latest_ts=latest)
        normalized = {key: float(features.get(key, 0.0) or 0.0) for key in _FEATURE_KEYS}
        if "adjusted_score" in features:
            normalized["adjusted_score"] = float(features["adjusted_score"])

        sample = TickSample(
            ts_utc=epoch,
            epic=str(epic),
            bid=float(bid),
            offer=float(offer),
            direction=str(direction or "WAIT"),
            features=normalized,
        )
        self.shadow.append(sample)
        self._maybe_apply_pending_swap()
        return self.live.score(normalized)

    def _maybe_apply_pending_swap(self) -> None:
        pending = self._pending_swap
        if pending is None:
            return
        candidate, telem = pending
        edge = float(telem.get("win_rate_edge") or 0.0)
        if edge <= _HOTSWAP_EDGE_THRESHOLD:
            return
        with self._swap_lock:
            self.live.atomic_swap(candidate)
            self._pending_swap = None
        with self.shadow._lock:
            self.shadow._telemetry.hot_swaps += 1
            self.shadow._telemetry.last_swap_at = time.time()
        log_engine(
            "TwinEngine: HOT-SWAP applied "
            f"v={candidate.version} edge={edge:.4f} "
            f"precision_drift={float(telem.get('precision_drift') or 0.0):.4f} "
            f"sortino_var={float(telem.get('sortino_variance') or 0.0):.6f}"
        )

    def _run_retrain_subprocess(
        self,
        snapshot: list[dict[str, Any]],
        live_precision: float,
        version: int,
    ) -> None:
        ctx = mp.get_context("spawn")
        out_queue: mp.Queue = ctx.Queue()
        payload = {
            "samples": snapshot,
            "live_precision": live_precision,
            "version": version,
            "trained_at": time.time(),
        }
        proc = ctx.Process(
            target=_train_weights_worker,
            args=(payload, out_queue),
            name="twin-engine-trainer",
            daemon=True,
        )
        try:
            proc.start()
            proc.join(timeout=120.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5.0)
                log_engine("TwinEngine: retrain subprocess timed out — fail-closed")
                return
            if out_queue.empty():
                return
            result = out_queue.get_nowait()
        except Exception as exc:
            log_guarded_exception("twin_engine_retrain", exc)
            return
        finally:
            with self.shadow._lock:
                self.shadow._retrain_inflight = False

        if not result.get("ok"):
            return

        telem = dict(result.get("telemetry") or {})
        weights_raw = result.get("weights") or {}
        candidate = ModelWeights(
            bias=float(weights_raw.get("bias") or 0.0),
            coeffs={
                k: float((weights_raw.get("coeffs") or {}).get(k, 0.0))
                for k in _FEATURE_KEYS
            },
            version=int(weights_raw.get("version") or 0),
            trained_at=float(weights_raw.get("trained_at") or time.time()),
        )

        with self.shadow._lock:
            self.shadow._telemetry.win_rate_edge = float(telem.get("win_rate_edge") or 0.0)
            self.shadow._telemetry.precision_drift = float(
                telem.get("precision_drift") or 0.0
            )
            self.shadow._telemetry.sortino_variance = float(
                telem.get("sortino_variance") or 0.0
            )
            self.shadow._telemetry.random_walk_baseline = float(
                telem.get("random_walk_baseline") or 0.0
            )
            self.shadow._telemetry.candidate_score = float(
                telem.get("candidate_score") or 0.0
            )
            self.shadow._telemetry.live_score = float(telem.get("live_score") or 0.0)
            self.shadow._telemetry.retrains += 1

        log_engine(
            "TwinEngine: retrain complete "
            f"edge={float(telem.get('win_rate_edge') or 0.0):.4f} "
            f"precision_drift={float(telem.get('precision_drift') or 0.0):.4f} "
            f"sortino_var={float(telem.get('sortino_variance') or 0.0):.6f} "
            f"rw_baseline={float(telem.get('random_walk_baseline') or 0.0):.4f}"
        )

        edge = float(telem.get("win_rate_edge") or 0.0)
        if edge > _HOTSWAP_EDGE_THRESHOLD:
            with self._swap_lock:
                self.live.atomic_swap(candidate)
            with self.shadow._lock:
                self.shadow._telemetry.hot_swaps += 1
                self.shadow._telemetry.last_swap_at = time.time()
            log_engine(
                f"TwinEngine: HOT-SWAP applied v={candidate.version} edge={edge:.4f}"
            )
            if os.environ.get("IG_PARALLEL_TRACK", "").strip() == "shadow":
                try:
                    from system.identity.weight_transfer_bridge import get_weight_transfer_bridge

                    get_weight_transfer_bridge(create=True).publish_candidate(
                        weights={
                            "bias": candidate.bias,
                            "coeffs": dict(candidate.coeffs),
                            "version": candidate.version,
                            "trained_at": candidate.trained_at,
                        },
                        edge=edge,
                        telemetry=telem,
                    )
                except Exception as exc:
                    log_guarded_exception("shadow_weight_transfer_publish", exc)
        else:
            log_engine(
                "TwinEngine: HOT-SWAP REJECTED "
                f"edge={edge:.4f} below random-walk threshold "
                f"{_HOTSWAP_EDGE_THRESHOLD:.4f} — live weights unchanged"
            )
            with self._swap_lock:
                self._pending_swap = (candidate, telem)

    def telemetry_dict(self) -> dict[str, Any]:
        tele = self.shadow.telemetry
        live = self.live.weights_snapshot()
        return {
            "live_model_version": live.version,
            "hot_swaps": tele.hot_swaps,
            "win_rate_edge": tele.win_rate_edge,
            "precision_drift": tele.precision_drift,
            "sortino_variance": tele.sortino_variance,
            "random_walk_baseline": tele.random_walk_baseline,
        }


_core_singleton: TwinEngineCore | None = None
_core_lock = threading.Lock()


def get_twin_engine_core() -> TwinEngineCore:
    global _core_singleton
    with _core_lock:
        if _core_singleton is None:
            _core_singleton = TwinEngineCore()
        return _core_singleton


def reset_twin_engine_core() -> None:
    """Drop singleton — integrity probes and tests only."""
    global _core_singleton
    with _core_lock:
        _core_singleton = None
