"""
Deterministic Cold-Start Compilation Kernel — pre-train LiveEngine weights offline.

Reads the 5-day production tick archive into in-memory arrays, computes look-back
features, runs sequential ``partial_fit`` sweeps, and writes an immutable warmed
alpha checkpoint consumed by Live Vanguard on PRODUCTION boot.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from system.engine_log import log_engine
from system.ml.twin_engine_core import LiveEngine, ModelWeights, _FEATURE_KEYS
from system.paths import data_dir, project_root

CHECKPOINT_FILENAME = "v30_warmed_alpha_weights.json"
EVALUATION_THRESHOLD_PCT = 51.5
PRODUCTION_WARMED_CONFIDENCE_FLOOR = 54.5
_DEFAULT_STOP_POINTS = 30.0
_RSI_PERIOD = 14
_ATR_PERIOD = 14
_PARTIAL_FIT_LR = 0.012
_APPLIED_FLAG = False


def warmed_alpha_checkpoint_path() -> Path:
    """Canonical repo checkpoint; falls back to active data_dir when mirrored."""
    canonical = project_root() / "src" / "data" / CHECKPOINT_FILENAME
    if canonical.is_file():
        return canonical
    runtime = data_dir() / CHECKPOINT_FILENAME
    if runtime.is_file():
        return runtime
    return canonical


def _checkpoint_write_paths() -> list[Path]:
    paths = [project_root() / "src" / "data" / CHECKPOINT_FILENAME]
    runtime = data_dir() / CHECKPOINT_FILENAME
    if runtime not in paths:
        paths.append(runtime)
    return paths


def default_archive_path() -> Path:
    bundled = (
        project_root()
        / "src"
        / "simulation"
        / "data"
        / "production_5day_archive.jsonl"
    )
    if bundled.is_file():
        return bundled
    return (
        Path(__file__).resolve().parent.parent.parent
        / "simulation"
        / "data"
        / "production_5day_archive.jsonl"
    )


@dataclass
class EpicArrayState:
    epic: str
    timestamps: array = field(default_factory=lambda: array("d"))
    bids: array = field(default_factory=lambda: array("d"))
    offers: array = field(default_factory=lambda: array("d"))
    mids: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    returns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))


def _parse_archive_ticks(path: Path) -> list[dict[str, Any]]:
    from simulation.historical_replayer import ReplayTick, _row_to_tick

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(raw.get("type") or "tick") not in ("tick", ""):
                continue
            tick = _row_to_tick(raw)
            if tick is None:
                continue
            rows.append(
                {
                    "epic": str(tick.epic),
                    "bid": float(tick.bid),
                    "offer": float(tick.offer),
                    "timestamp": float(tick.timestamp),
                }
            )
    return rows


def _build_epic_arrays(rows: list[dict[str, Any]]) -> dict[str, EpicArrayState]:
    grouped: dict[str, EpicArrayState] = {}
    for row in rows:
        epic = str(row["epic"])
        state = grouped.get(epic)
        if state is None:
            state = EpicArrayState(epic=epic)
            grouped[epic] = state
        state.timestamps.append(float(row["timestamp"]))
        state.bids.append(float(row["bid"]))
        state.offers.append(float(row["offer"]))
    for epic, state in grouped.items():
        bids = np.frombuffer(state.bids, dtype=np.float64)
        offers = np.frombuffer(state.offers, dtype=np.float64)
        state.mids = (bids + offers) * 0.5
        if state.mids.size > 1:
            prev = state.mids[:-1]
            cur = state.mids[1:]
            safe_prev = np.where(prev != 0.0, prev, 1.0)
            rets = (cur - prev) / safe_prev
            padded = np.zeros(state.mids.size, dtype=np.float64)
            padded[1:] = rets
            state.returns = padded
        else:
            state.returns = np.zeros(state.mids.size, dtype=np.float64)
    return grouped


def _compute_rsi(mids: np.ndarray, index: int, period: int = _RSI_PERIOD) -> float:
    if index < period:
        return 50.0
    window = mids[index - period + 1 : index + 1]
    deltas = np.diff(window)
    gains = np.clip(deltas, 0.0, None)
    losses = np.clip(-deltas, 0.0, None)
    avg_gain = float(np.mean(gains)) if gains.size else 0.0
    avg_loss = float(np.mean(losses)) if losses.size else 0.0
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_atr(bids: np.ndarray, offers: np.ndarray, index: int, period: int = _ATR_PERIOD) -> float:
    if index < 1:
        return 0.0
    start = max(1, index - period + 1)
    tr_values: list[float] = []
    for idx in range(start, index + 1):
        high = float(max(bids[idx], offers[idx]))
        low = float(min(bids[idx], offers[idx]))
        prev_close = float((bids[idx - 1] + offers[idx - 1]) * 0.5)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    return float(statistics.fmean(tr_values)) if tr_values else 0.0


def _adjusted_score_from_features(*, rsi: float, atr_ratio: float, momentum: float) -> float:
    rsi_component = max(0.0, min(100.0, rsi))
    atr_component = max(0.0, min(100.0, 50.0 + (atr_ratio - 1.0) * 25.0))
    momentum_component = max(0.0, min(100.0, 50.0 + momentum * 5000.0))
    return float(
        0.45 * rsi_component + 0.30 * atr_component + 0.25 * momentum_component
    )


def _feature_gradients(
    features: dict[str, float],
    *,
    label: float,
    prediction: float,
) -> dict[str, float]:
    error = float(label) - float(prediction)
    return {key: error * float(features.get(key, 0.0)) for key in _FEATURE_KEYS}


def compile_warmed_alpha_weights(
    *,
    archive_path: Path | None = None,
    checkpoint_path: Path | None = None,
    evaluation_threshold_pct: float = EVALUATION_THRESHOLD_PCT,
    stop_points: float = _DEFAULT_STOP_POINTS,
) -> dict[str, Any]:
    """Fast-forward compilation — sequential partial_fit over the full archive."""
    src = archive_path or default_archive_path()
    dst = checkpoint_path or warmed_alpha_checkpoint_path()
    if not src.is_file():
        raise FileNotFoundError(f"cold-start archive missing: {src}")

    t0 = time.perf_counter()
    rows = _parse_archive_ticks(src)
    if not rows:
        raise RuntimeError(f"cold-start archive empty: {src}")

    epic_arrays = _build_epic_arrays(rows)
    epic_index: dict[str, int] = {epic: 0 for epic in epic_arrays}
    engine = LiveEngine()

    samples_processed = 0
    evaluation_passes = 0
    feature_accum: dict[str, list[float]] = {key: [] for key in _FEATURE_KEYS}

    for row in rows:
        epic = str(row["epic"])
        state = epic_arrays.get(epic)
        if state is None:
            continue
        idx = epic_index[epic]
        epic_index[epic] = idx + 1

        bids = np.frombuffer(state.bids, dtype=np.float64)
        offers = np.frombuffer(state.offers, dtype=np.float64)
        mid = float(state.mids[idx]) if idx < state.mids.size else 0.0
        if mid <= 0.0:
            continue

        rsi = _compute_rsi(state.mids, idx)
        atr = _compute_atr(bids, offers, idx)
        atr_ratio = atr / max(1.0, float(stop_points))
        momentum = float(state.returns[idx]) if idx < state.returns.size else 0.0
        adjusted_score = _adjusted_score_from_features(
            rsi=rsi,
            atr_ratio=atr_ratio,
            momentum=momentum,
        )
        features = {
            "adjusted_score": adjusted_score,
            "rsi": rsi,
            "atr_ratio": atr_ratio,
        }

        prediction = engine.score(features)
        label = 1.0 if momentum > 0.0 else 0.0
        engine.partial_fit(
            _feature_gradients(features, label=label, prediction=prediction),
            learning_rate=_PARTIAL_FIT_LR,
        )

        for key in _FEATURE_KEYS:
            feature_accum[key].append(float(features[key]))

        prob_pct = prediction * 100.0
        blended_pct = 0.65 * adjusted_score + 0.35 * prob_pct
        if blended_pct >= float(evaluation_threshold_pct):
            evaluation_passes += 1

        samples_processed += 1

    weights = engine.weights_snapshot()
    elapsed = time.perf_counter() - t0
    top_indicators = _build_top_indicators(weights)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "compiler": "cold_start_compiler",
        "compiled_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compiled_at_epoch": time.time(),
        "archive_path": str(src),
        "archive_ticks": len(rows),
        "samples_processed": samples_processed,
        "evaluation_threshold_pct": float(evaluation_threshold_pct),
        "evaluation_passes": evaluation_passes,
        "production_confidence_floor_pct": PRODUCTION_WARMED_CONFIDENCE_FLOOR,
        "compilation_elapsed_sec": round(elapsed, 3),
        "weights": {
            "bias": float(weights.bias),
            "coeffs": {key: float(weights.coeffs.get(key, 0.0)) for key in _FEATURE_KEYS},
            "version": int(weights.version),
            "trained_at": float(weights.trained_at or time.time()),
        },
        "feature_stats": {
            key: {
                "mean": round(float(statistics.fmean(values)), 6) if values else 0.0,
                "stdev": round(float(statistics.pstdev(values)), 6)
                if len(values) > 1
                else 0.0,
            }
            for key, values in feature_accum.items()
        },
        "top_indicators": top_indicators,
    }

    dst.parent.mkdir(parents=True, exist_ok=True)
    payload_text = json.dumps(manifest, indent=2)
    for target in _checkpoint_write_paths():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload_text, encoding="utf-8")
        tmp.replace(target)
    dst = warmed_alpha_checkpoint_path()

    log_engine(
        "ColdStartCompiler: warmed alpha checkpoint written "
        f"path={dst} samples={samples_processed} passes={evaluation_passes} "
        f"elapsed={elapsed:.2f}s version={weights.version}"
    )
    return manifest


def _build_top_indicators(weights: ModelWeights) -> list[dict[str, Any]]:
    ranked = sorted(
        ((key, float(weights.coeffs.get(key, 0.0))) for key in _FEATURE_KEYS),
        key=lambda item: abs(item[1]),
        reverse=True,
    )
    indicators: list[dict[str, Any]] = []
    label_map = {
        "adjusted_score": "Adjusted Score",
        "rsi": "RSI Momentum",
        "atr_ratio": "ATR Regime Ratio",
    }
    for key, coeff in ranked:
        indicators.append(
            {
                "name": label_map.get(key, key),
                "feature": key,
                "weight": round(coeff, 6),
                "weight_pct": round(min(100.0, abs(coeff) * 100.0), 1),
                "delta": round(coeff, 6),
                "direction": "positive" if coeff >= 0 else "negative",
            }
        )
    return indicators


def load_warmed_alpha_manifest(*, checkpoint_path: Path | None = None) -> dict[str, Any] | None:
    path = checkpoint_path or warmed_alpha_checkpoint_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def manifest_to_model_weights(manifest: dict[str, Any]) -> ModelWeights:
    weights_raw = manifest.get("weights") or {}
    coeffs_raw = weights_raw.get("coeffs") or {}
    return ModelWeights(
        bias=float(weights_raw.get("bias") or 0.0),
        coeffs={key: float(coeffs_raw.get(key, 0.0)) for key in _FEATURE_KEYS},
        version=int(weights_raw.get("version") or 0),
        trained_at=float(weights_raw.get("trained_at") or time.time()),
    )


def inject_warmed_alpha_weights(*, checkpoint_path: Path | None = None) -> bool:
    """Load checkpoint and atomic-swap into the process-local LiveEngine."""
    global _APPLIED_FLAG
    manifest = load_warmed_alpha_manifest(checkpoint_path=checkpoint_path)
    if manifest is None:
        return False
    model = manifest_to_model_weights(manifest)
    if model.version <= 0:
        return False
    from system.ml.twin_engine_core import get_twin_engine_core

    get_twin_engine_core().live.atomic_swap(model)
    _APPLIED_FLAG = True
    log_engine(
        "ColdStartCompiler: LiveEngine warmed alpha injected "
        f"version={model.version} checkpoint={checkpoint_path or warmed_alpha_checkpoint_path()}"
    )
    return True


def production_warmed_alpha_active() -> bool:
    if _APPLIED_FLAG:
        return True
    manifest = load_warmed_alpha_manifest()
    if manifest is None:
        return False
    try:
        from system.ml.twin_engine_core import get_twin_engine_core

        live = get_twin_engine_core().live.weights_snapshot()
        expected_version = int((manifest.get("weights") or {}).get("version") or 0)
        return expected_version > 0 and live.version >= expected_version
    except Exception:
        return False


def production_warmed_confidence_floor() -> float:
    return PRODUCTION_WARMED_CONFIDENCE_FLOOR


def ml_optimization_from_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return {}
    weights = manifest.get("weights") or {}
    return {
        "warmed_alpha": True,
        "compiler": str(manifest.get("compiler") or "cold_start_compiler"),
        "compiled_at_utc": manifest.get("compiled_at_utc"),
        "compiled_at_epoch": manifest.get("compiled_at_epoch"),
        "samples_processed": int(manifest.get("samples_processed") or 0),
        "evaluation_threshold_pct": float(manifest.get("evaluation_threshold_pct") or 0.0),
        "evaluation_passes": int(manifest.get("evaluation_passes") or 0),
        "production_confidence_floor_pct": float(
            manifest.get("production_confidence_floor_pct")
            or PRODUCTION_WARMED_CONFIDENCE_FLOOR
        ),
        "live_model_version": int(weights.get("version") or 0),
        "top_indicators": list(manifest.get("top_indicators") or []),
        "feature_stats": dict(manifest.get("feature_stats") or {}),
        "last_review_outcome": "warmed_alpha_compiled",
        "risk_scalar": 1.0,
        "vol_threshold_multiplier": 1.0,
        "size_scalar": 1.0,
        "stop_tighten_scalar": 1.0,
        "last_review_cycle": int(weights.get("version") or 0),
    }


def main() -> int:
    manifest = compile_warmed_alpha_weights()
    print(
        json.dumps(
            {
                "ok": True,
                "checkpoint": str(warmed_alpha_checkpoint_path()),
                "samples_processed": manifest.get("samples_processed"),
                "evaluation_passes": manifest.get("evaluation_passes"),
                "version": (manifest.get("weights") or {}).get("version"),
                "elapsed_sec": manifest.get("compilation_elapsed_sec"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
