"""
Matrix Backtuner — instant combinatorial gate-floor optimizer.

Maps shadow_log signal entries against the static 5-day production tick archive,
labels each as True Win / True Loss via first-touch ATR stop/TP resolution, sweeps
signal_confidence / ml_veto / environment_fitness floors, and promotes the sweet
spot to config_v29.json + v30_warmed_alpha_weights.json.

Offline only — safe to run before session open; does not touch live processes.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np

from data.ohlc_yahoo_seeder import EPIC_YAHOO_MAP
from system.config_loader import ConfigLoader
from system.engine_log import log_engine
from system.ml.cold_start_compiler import (
    CHECKPOINT_FILENAME,
    PRODUCTION_WARMED_CONFIDENCE_FLOOR,
    _checkpoint_write_paths,
    default_archive_path,
    load_warmed_alpha_manifest,
    manifest_to_model_weights,
    warmed_alpha_checkpoint_path,
)
from system.ml.twin_engine_core import ModelWeights, _FEATURE_KEYS
from system.paths import data_dir, project_root

OutcomeTag = Literal["true_win", "true_loss", "unresolved"]

MARKET_TO_EPIC: dict[str, str] = {
    market: epic for epic, (_sym, market) in EPIC_YAHOO_MAP.items()
}

DEFAULT_EPIC_STOP: dict[str, float] = {
    "CS.D.EURUSD.CFD.IP": 5.0,
    "CS.D.CFPGOLD.CFP.IP": 5.0,
    "IX.D.NIKKEI.IFM.IP": 30.0,
    "IX.D.DOW.IFM.IP": 45.0,
    "IX.D.NASDAQ.IFM.IP": 45.0,
}

SWEEP_STEPS = 101  # -5.0% … +5.0% of base floor in 0.1% increments
MAX_FORWARD_TICKS = 600
REPORT_FILENAME = "matrix_backtuner_report.json"


@dataclass
class FloorBases:
    signal_confidence_pct: float
    environment_fitness_pct: float
    ml_veto_probability: float


@dataclass
class LabeledSignal:
    timestamp: str
    market: str
    epic: str
    direction: str
    confidence: float
    fitness: float
    ml_probability: float
    rsi: float
    atr: float
    outcome: OutcomeTag
    entry_idx: int
    stop_points: float
    take_profit_points: float
    gate_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class SweepResult:
    signal_confidence_floor_pct: float
    environment_fitness_floor_pct: float
    ml_veto_floor_probability: float
    true_wins_included: int
    true_losses_included: int
    signals_selected: int
    score: float


def _config_v29_path() -> Path:
    return project_root() / "config" / "config_v29.json"


def _shadow_log_path() -> Path:
    return data_dir() / "shadow_log.jsonl"


def _report_path() -> Path:
    return data_dir() / REPORT_FILENAME


def _load_merged_config() -> dict[str, Any]:
    loader = ConfigLoader()
    cfg = loader.load()
    return cfg if isinstance(cfg, dict) else {}


def _config_v29_dict() -> dict[str, Any]:
    path = _config_v29_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_floor_bases(cfg: dict[str, Any]) -> FloorBases:
    v29 = _config_v29_dict()
    prot = v29.get("protective_learning") or cfg.get("protective_learning") or {}
    soak = v29.get("demo_soak_mode") or cfg.get("demo_soak_mode") or {}
    ml = v29.get("ml_veto") or cfg.get("ml_veto") or {}
    conf = float(
        prot.get("signal_threshold_floor")
        or cfg.get("signal_threshold")
        or 52.5
    )
    fitness = float(
        soak.get("fitness_min")
        or prot.get("fitness_min_floor")
        or 52.5
    )
    ml_prob = float(ml.get("min_probability") or 0.45)
    return FloorBases(
        signal_confidence_pct=conf,
        environment_fitness_pct=fitness,
        ml_veto_probability=ml_prob,
    )


def _reward_multiple(cfg: dict[str, Any]) -> float:
    try:
        return float(cfg.get("reward_multiple") or 3.0)
    except (TypeError, ValueError):
        return 3.0


@dataclass
class EpicTape:
    epic: str
    mids: np.ndarray
    bids: np.ndarray
    offers: np.ndarray


def load_archive_tapes(archive_path: Path) -> dict[str, EpicTape]:
    from system.ml.cold_start_compiler import _build_epic_arrays, _parse_archive_ticks

    rows = _parse_archive_ticks(archive_path)
    if not rows:
        raise RuntimeError(f"archive empty: {archive_path}")
    arrays = _build_epic_arrays(rows)
    tapes: dict[str, EpicTape] = {}
    for epic, state in arrays.items():
        tapes[epic] = EpicTape(
            epic=epic,
            mids=state.mids.copy(),
            bids=np.frombuffer(state.bids, dtype=np.float64).copy(),
            offers=np.frombuffer(state.offers, dtype=np.float64).copy(),
        )
    return tapes


def load_archive_mids(archive_path: Path) -> dict[str, np.ndarray]:
    return {epic: tape.mids for epic, tape in load_archive_tapes(archive_path).items()}


def _load_model_weights() -> ModelWeights:
    manifest = load_warmed_alpha_manifest()
    if manifest:
        return manifest_to_model_weights(manifest)
    return ModelWeights()


def _archive_features_at(
    tape: EpicTape,
    index: int,
    *,
    stop_points: float,
) -> tuple[float, float, float]:
    from system.ml.cold_start_compiler import _compute_atr, _compute_rsi, _adjusted_score_from_features

    rsi = _compute_rsi(tape.mids, index)
    atr = _compute_atr(tape.bids, tape.offers, index)
    atr_ratio = atr / max(1.0, stop_points)
    momentum = 0.0
    if index > 0 and tape.mids[index - 1] > 0:
        momentum = (float(tape.mids[index]) - float(tape.mids[index - 1])) / float(
            tape.mids[index - 1]
        )
    conf = _adjusted_score_from_features(
        rsi=rsi, atr_ratio=atr_ratio, momentum=momentum
    )
    return float(conf), float(rsi), float(atr)


@dataclass
class EpicFeatureCache:
    rsi: np.ndarray
    atr: np.ndarray
    confidence: np.ndarray


def _build_epic_feature_cache(tape: EpicTape, *, stop_points: float) -> EpicFeatureCache:
    n = tape.mids.size
    rsi_vals = np.empty(n, dtype=np.float64)
    atr_vals = np.empty(n, dtype=np.float64)
    conf_vals = np.empty(n, dtype=np.float64)
    for idx in range(n):
        conf, rsi, atr = _archive_features_at(tape, idx, stop_points=stop_points)
        rsi_vals[idx] = rsi
        atr_vals[idx] = atr
        conf_vals[idx] = conf
    return EpicFeatureCache(rsi=rsi_vals, atr=atr_vals, confidence=conf_vals)


def _match_archive_entry_idx_cached(
    cache: EpicFeatureCache,
    tape: EpicTape,
    *,
    target_rsi: float,
    target_atr: float,
    max_forward: int = MAX_FORWARD_TICKS,
    stride: int = 8,
) -> int:
    limit = max(32, tape.mids.size - max_forward - 1)
    if limit <= 32:
        return 16
    indices = np.arange(32, limit, stride, dtype=np.int64)
    scores = np.abs(cache.rsi[indices] - float(target_rsi)) + np.abs(
        cache.atr[indices] - max(0.0, float(target_atr))
    ) * 2.0
    return int(indices[int(np.argmin(scores))])


def _simulated_environment_fitness(mids: np.ndarray, entry_idx: int, *, window: int = 48) -> float:
    """Archive-anchored fitness proxy — varies with local trend/vol on the static tape."""
    if entry_idx < window or entry_idx >= mids.size:
        return 55.0
    seg = mids[entry_idx - window : entry_idx + 1]
    if seg.size < 2:
        return 55.0
    mean_px = float(np.mean(seg))
    if mean_px <= 0:
        return 55.0
    vol_score = min(25.0, float(np.std(seg) / mean_px) * 10_000.0)
    trend_score = max(-20.0, min(20.0, ((float(seg[-1]) - float(seg[0])) / mean_px) * 5000.0))
    spread_stability = max(0.0, 15.0 - vol_score * 0.35)
    return float(max(0.0, min(100.0, 45.0 + trend_score + spread_stability)))


def _directional_ml_probability(
    weights: ModelWeights,
    *,
    direction: str,
    confidence: float,
    rsi: float,
    atr: float,
    epic: str,
    cfg: dict[str, Any],
) -> float:
    stop = float(
        DEFAULT_EPIC_STOP.get(epic)
        or cfg.get("stop_distance_points")
        or 30.0
    )
    atr_ratio = float(atr) / max(1.0, stop) if atr > 0 else 0.0
    features = {
        "adjusted_score": float(confidence),
        "rsi": float(rsi),
        "atr_ratio": float(atr_ratio),
    }
    base = float(weights.score(features))
    side = str(direction or "").upper()
    if side == "BUY":
        rsi_edge = max(0.0, min(1.0, (68.0 - float(rsi)) / 38.0))
    elif side == "SELL":
        rsi_edge = max(0.0, min(1.0, (float(rsi) - 32.0) / 38.0))
    else:
        rsi_edge = 0.0
    conf_edge = max(0.0, min(1.0, float(confidence) / 100.0))
    return float(max(0.0, min(1.0, 0.45 * base + 0.35 * rsi_edge + 0.20 * conf_edge)))


def _ml_probability(
    weights: ModelWeights,
    *,
    direction: str,
    confidence: float,
    rsi: float,
    atr: float,
    epic: str,
    cfg: dict[str, Any],
) -> float:
    return _directional_ml_probability(
        weights,
        direction=direction,
        confidence=confidence,
        rsi=rsi,
        atr=atr,
        epic=epic,
        cfg=cfg,
    )


def _stop_and_tp(
    *,
    epic: str,
    atr: float,
    cfg: dict[str, Any],
    reward_multiple: float,
) -> tuple[float, float]:
    stop = float(atr) if atr > 0 else float(
        DEFAULT_EPIC_STOP.get(epic)
        or cfg.get("stop_distance_points")
        or 30.0
    )
    stop = max(0.5, stop)
    tp = stop * reward_multiple
    return stop, tp


_ARCHIVE_TAPES_CACHE: dict[str, EpicTape] | None = None


def _get_archive_tapes() -> dict[str, EpicTape]:
    global _ARCHIVE_TAPES_CACHE
    if _ARCHIVE_TAPES_CACHE is None:
        _ARCHIVE_TAPES_CACHE = load_archive_tapes(default_archive_path())
    return _ARCHIVE_TAPES_CACHE


def evaluate_archive_lookahead_outcome(
    epic: str,
    direction: str,
    *,
    rsi: float = 0.0,
    atr: float = 0.0,
    cfg: dict[str, Any] | None = None,
) -> tuple[OutcomeTag, dict[str, Any]]:
    """Match live signal features on the static archive and resolve first-touch TP/SL."""
    side = str(direction or "").upper()
    epic_key = str(epic or "").strip()
    if side not in ("BUY", "SELL") or not epic_key:
        return "unresolved", {"reason": "invalid_direction_or_epic"}

    active_cfg = cfg if cfg is not None else _load_merged_config()
    reward_multiple = _reward_multiple(active_cfg)
    try:
        tapes = _get_archive_tapes()
    except Exception as exc:
        return "unresolved", {"reason": "archive_load_failed", "error": str(exc)}

    tape = tapes.get(epic_key)
    if tape is None or tape.mids.size < 32:
        return "unresolved", {"reason": "missing_epic_tape", "epic": epic_key}

    stop_pts, tp_pts = _stop_and_tp(
        epic=epic_key,
        atr=float(atr),
        cfg=active_cfg,
        reward_multiple=reward_multiple,
    )
    cache = _build_epic_feature_cache(tape, stop_points=stop_pts)
    entry_idx = _match_archive_entry_idx_cached(
        cache,
        tape,
        target_rsi=float(rsi),
        target_atr=float(atr),
    )
    outcome = resolve_first_touch_outcome(
        side,
        tape.mids,
        entry_idx,
        stop_pts=stop_pts,
        tp_pts=tp_pts,
    )
    return outcome, {
        "epic": epic_key,
        "direction": side,
        "entry_idx": entry_idx,
        "stop_pts": stop_pts,
        "tp_pts": tp_pts,
        "rsi": float(rsi),
        "atr": float(atr),
        "outcome": outcome,
    }


def reset_archive_lookahead_cache_for_tests() -> None:
    global _ARCHIVE_TAPES_CACHE
    _ARCHIVE_TAPES_CACHE = None


def resolve_first_touch_outcome(
    direction: str,
    mids: np.ndarray,
    entry_idx: int,
    *,
    stop_pts: float,
    tp_pts: float,
    max_ticks: int = MAX_FORWARD_TICKS,
) -> OutcomeTag:
    if entry_idx < 0 or entry_idx >= mids.size:
        return "unresolved"
    entry = float(mids[entry_idx])
    if entry <= 0:
        return "unresolved"
    end = min(mids.size, entry_idx + max_ticks)
    side = str(direction or "").upper()
    if side not in ("BUY", "SELL"):
        return "unresolved"

    for i in range(entry_idx + 1, end):
        mid = float(mids[i])
        if side == "BUY":
            if mid >= entry + tp_pts:
                return "true_win"
            if mid <= entry - stop_pts:
                return "true_loss"
        else:
            if mid <= entry - tp_pts:
                return "true_win"
            if mid >= entry + stop_pts:
                return "true_loss"
    return "unresolved"


def _load_gate_snapshots_by_ts() -> dict[str, dict[str, Any]]:
    """Optional merge from shadow_ledger rows that carry gate_snapshot."""
    ledger = data_dir() / "shadow_ledger.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not ledger.is_file():
        return out
    try:
        with ledger.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                snap = row.get("gate_snapshot")
                ts = str(row.get("timestamp") or row.get("ts") or "")
                if isinstance(snap, dict) and ts:
                    out[ts[:19]] = snap
    except OSError:
        pass
    return out


def load_shadow_signals(
    shadow_path: Path,
    *,
    cfg: dict[str, Any],
    weights: ModelWeights,
    archive_tapes: dict[str, EpicTape],
    reward_multiple: float,
) -> list[LabeledSignal]:
    if not shadow_path.is_file():
        raise FileNotFoundError(f"shadow log missing: {shadow_path}")

    ledger_snaps = _load_gate_snapshots_by_ts()
    labeled: list[LabeledSignal] = []
    feature_cache: dict[str, EpicFeatureCache] = {}

    with shadow_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            direction = str(row.get("direction") or "").upper()
            if direction not in ("BUY", "SELL"):
                continue
            if not bool(row.get("would_have_fired")):
                continue

            market = str(row.get("market") or "")
            epic = MARKET_TO_EPIC.get(market, "")
            tape = archive_tapes.get(epic)
            if epic == "" or tape is None or tape.mids.size < 32:
                continue

            shadow_confidence = float(row.get("adjusted_score") or row.get("confidence") or 0)
            shadow_fitness = float(row.get("fitness") or 0)
            rsi = float(row.get("rsi") or 0)
            atr = float(row.get("atr") or 0)
            ts = str(row.get("timestamp") or "")

            stop_pts, tp_pts = _stop_and_tp(
                epic=epic, atr=atr, cfg=cfg, reward_multiple=reward_multiple
            )
            cache = feature_cache.get(epic)
            if cache is None:
                cache = _build_epic_feature_cache(tape, stop_points=stop_pts)
                feature_cache[epic] = cache
            entry_idx = _match_archive_entry_idx_cached(
                cache,
                tape,
                target_rsi=rsi,
                target_atr=atr,
            )
            archive_conf, archive_rsi, archive_atr = _archive_features_at(
                tape, entry_idx, stop_points=stop_pts
            )
            archive_fitness = _simulated_environment_fitness(tape.mids, entry_idx)
            outcome = resolve_first_touch_outcome(
                direction,
                tape.mids,
                entry_idx,
                stop_pts=stop_pts,
                tp_pts=tp_pts,
            )
            ml_prob = _ml_probability(
                weights,
                direction=direction,
                confidence=archive_conf,
                rsi=archive_rsi,
                atr=archive_atr,
                epic=epic,
                cfg=cfg,
            )

            gate_snapshot = dict(ledger_snaps.get(ts[:19], {}))
            gate_snapshot.update(
                {
                    "signal_confidence": archive_conf,
                    "shadow_confidence": shadow_confidence,
                    "environment_fitness": archive_fitness,
                    "shadow_log_fitness": shadow_fitness,
                    "ml_veto_probability": round(ml_prob, 4),
                    "archive_rsi": round(archive_rsi, 2),
                    "archive_atr": round(archive_atr, 4),
                    "direction": direction,
                    "would_have_fired": True,
                    "gate_blocked_at": row.get("gate_blocked_at"),
                    "session": row.get("session"),
                    "setup_key": row.get("setup_key"),
                    "entry_idx": entry_idx,
                }
            )

            labeled.append(
                LabeledSignal(
                    timestamp=ts,
                    market=market,
                    epic=epic,
                    direction=direction,
                    confidence=archive_conf,
                    fitness=archive_fitness,
                    ml_probability=ml_prob,
                    rsi=rsi,
                    atr=atr,
                    outcome=outcome,
                    entry_idx=entry_idx,
                    stop_points=stop_pts,
                    take_profit_points=tp_pts,
                    gate_snapshot=gate_snapshot,
                )
            )
    return labeled


def _sweep_multipliers() -> np.ndarray:
    return np.linspace(0.95, 1.05, SWEEP_STEPS)


def _config_local_floor_grid(base: float, *, probability: bool = False) -> np.ndarray:
    return base * _sweep_multipliers()


def _expanded_floor_grid(
    values: np.ndarray,
    resolved: np.ndarray,
    *,
    steps: int = SWEEP_STEPS,
) -> np.ndarray:
    subset = values[resolved]
    if subset.size == 0:
        return np.array([], dtype=np.float64)
    lo = float(np.percentile(subset, 5))
    hi = float(np.percentile(subset, 95))
    if math.isclose(lo, hi):
        return np.array([lo], dtype=np.float64)
    return np.linspace(lo, hi, steps)


def _evaluate_sweep_grid(
    *,
    conf: np.ndarray,
    fitness: np.ndarray,
    ml: np.ndarray,
    is_win: np.ndarray,
    is_loss: np.ndarray,
    resolved: np.ndarray,
    conf_floors: np.ndarray,
    fit_floors: np.ndarray,
    ml_floors: np.ndarray,
) -> tuple[SweepResult | None, list[SweepResult]]:
    best: SweepResult | None = None
    top: list[SweepResult] = []

    for conf_floor in conf_floors:
        conf_pass = conf >= conf_floor
        for fit_floor in fit_floors:
            fit_pass = fitness >= fit_floor
            layer = conf_pass & fit_pass & resolved
            for ml_floor in ml_floors:
                sel = layer & (ml >= ml_floor)
                n_sel = int(sel.sum())
                if n_sel == 0:
                    continue
                n_loss = int((sel & is_loss).sum())
                if n_loss > 0:
                    continue
                n_win = int((sel & is_win).sum())
                score = float(n_win) - 0.001 * n_sel
                candidate = SweepResult(
                    signal_confidence_floor_pct=round(float(conf_floor), 3),
                    environment_fitness_floor_pct=round(float(fit_floor), 3),
                    ml_veto_floor_probability=round(float(ml_floor), 4),
                    true_wins_included=n_win,
                    true_losses_included=n_loss,
                    signals_selected=n_sel,
                    score=score,
                )
                top.append(candidate)
                if best is None or candidate.score > best.score:
                    best = candidate
                elif (
                    candidate.score == best.score
                    and candidate.signals_selected > best.signals_selected
                ):
                    best = candidate

    top.sort(key=lambda r: (-r.score, -r.true_wins_included, r.signals_selected))
    return best, top[:25]


def sweep_threshold_matrix(
    signals: list[LabeledSignal],
    bases: FloorBases,
) -> tuple[SweepResult | None, list[SweepResult], dict[str, Any]]:
    if not signals:
        return None, [], {"phase": "none"}

    conf = np.array([s.confidence for s in signals], dtype=np.float64)
    fitness = np.array([s.fitness for s in signals], dtype=np.float64)
    ml = np.array([s.ml_probability for s in signals], dtype=np.float64)
    is_win = np.array([s.outcome == "true_win" for s in signals], dtype=bool)
    is_loss = np.array([s.outcome == "true_loss" for s in signals], dtype=bool)
    resolved = is_win | is_loss

    meta: dict[str, Any] = {"phases": []}

    best, top = _evaluate_sweep_grid(
        conf=conf,
        fitness=fitness,
        ml=ml,
        is_win=is_win,
        is_loss=is_loss,
        resolved=resolved,
        conf_floors=_config_local_floor_grid(bases.signal_confidence_pct),
        fit_floors=_config_local_floor_grid(bases.environment_fitness_pct),
        ml_floors=_config_local_floor_grid(bases.ml_veto_probability, probability=True),
    )
    meta["phases"].append(
        {
            "name": "config_local",
            "range": "±5.0% of production floors",
            "best_found": best is not None,
            "candidates": len(top),
        }
    )

    if best is None:
        best, top = _evaluate_sweep_grid(
            conf=conf,
            fitness=fitness,
            ml=ml,
            is_win=is_win,
            is_loss=is_loss,
            resolved=resolved,
            conf_floors=_expanded_floor_grid(conf, resolved),
            fit_floors=_expanded_floor_grid(fitness, resolved),
            ml_floors=_expanded_floor_grid(ml, resolved),
        )
        meta["phases"].append(
            {
                "name": "archive_expanded",
                "range": "P5–P95 of archive-matched gate metrics",
                "best_found": best is not None,
                "candidates": len(top),
            }
        )
        meta["phase_used"] = "archive_expanded"
    else:
        meta["phase_used"] = "config_local"

    return best, top, meta


def promote_floors(
    best: SweepResult,
    *,
    bases: FloorBases,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg_path = _config_v29_path()
    cfg_payload = json.loads(cfg_path.read_text(encoding="utf-8"))

    prot = dict(cfg_payload.get("protective_learning") or {})
    prot["signal_threshold_floor"] = round(best.signal_confidence_floor_pct, 2)
    prot["fitness_min_floor"] = round(best.environment_fitness_floor_pct, 2)
    cfg_payload["protective_learning"] = prot

    soak = dict(cfg_payload.get("demo_soak_mode") or {})
    soak["fitness_min"] = round(best.environment_fitness_floor_pct, 2)
    cfg_payload["demo_soak_mode"] = soak

    ml_overlay = dict(cfg_payload.get("ml_veto") or {})
    ml_overlay["min_probability"] = round(best.ml_veto_floor_probability, 4)
    ml_overlay["enabled"] = True
    cfg_payload["ml_veto"] = ml_overlay

    cfg_payload["matrix_backtuner"] = {
        "promoted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_floors": asdict(bases),
        "optimal_floors": asdict(best),
        "optimizer": "matrix_backtuner",
        "_note": "Offline combinatorial sweep — True Loss exclusion enforced",
    }

    manifest = load_warmed_alpha_manifest() or {}
    weights_raw = dict(manifest.get("weights") or {})
    coeffs = dict(weights_raw.get("coeffs") or {})
    version = int(weights_raw.get("version") or 0) + 1
    trained_at = time.time()

    manifest.update(
        {
            "schema_version": manifest.get("schema_version") or "1.0",
            "compiler": "matrix_backtuner",
            "compiled_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "compiled_at_epoch": trained_at,
            "production_confidence_floor_pct": round(
                best.signal_confidence_floor_pct, 2
            ),
            "evaluation_threshold_pct": round(
                max(45.0, best.signal_confidence_floor_pct - 3.0), 2
            ),
            "matrix_backtuner": {
                "promoted_at_utc": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "optimal_floors": asdict(best),
                "baseline_floors": asdict(bases),
            },
            "weights": {
                "bias": float(weights_raw.get("bias") or 0.0),
                "coeffs": {k: float(coeffs.get(k, 0.0)) for k in _FEATURE_KEYS},
                "version": version,
                "trained_at": trained_at,
            },
        }
    )
    if "top_indicators" not in manifest and coeffs:
        manifest["top_indicators"] = [
            {
                "name": key.replace("_", " ").title(),
                "feature": key,
                "weight": round(float(coeffs.get(key, 0.0)), 6),
            }
            for key in _FEATURE_KEYS
        ]

    promotion: dict[str, Any] = {
        "config_v29": str(cfg_path),
        "warmed_alpha_paths": [str(p) for p in _checkpoint_write_paths()],
        "optimal": asdict(best),
    }

    baseline = asdict(bases)
    optimal = asdict(best)
    promotion["deviations_from_baseline"] = {
        "signal_confidence_pct_delta": round(
            optimal["signal_confidence_floor_pct"] - baseline["signal_confidence_pct"], 3
        ),
        "environment_fitness_pct_delta": round(
            optimal["environment_fitness_floor_pct"] - baseline["environment_fitness_pct"],
            3,
        ),
        "ml_veto_probability_delta": round(
            optimal["ml_veto_floor_probability"] - baseline["ml_veto_probability"], 4
        ),
    }

    if dry_run:
        promotion["dry_run"] = True
        return promotion

    cfg_path.write_text(
        json.dumps(cfg_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    payload_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    for path in _checkpoint_write_paths():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload_text, encoding="utf-8")

    log_engine(
        "MatrixBacktuner: promoted floors "
        f"conf={best.signal_confidence_floor_pct}% "
        f"fitness={best.environment_fitness_floor_pct}% "
        f"ml_veto={best.ml_veto_floor_probability} "
        f"wins={best.true_wins_included} selected={best.signals_selected}"
    )
    return promotion


def run_matrix_backtuner(
    *,
    archive_path: Path | None = None,
    shadow_path: Path | None = None,
    promote: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    archive = archive_path or default_archive_path()
    shadow = shadow_path or _shadow_log_path()
    cfg = _load_merged_config()
    bases = resolve_floor_bases(cfg)
    reward_multiple = _reward_multiple(cfg)
    weights = _load_model_weights()

    archive_tapes = load_archive_tapes(archive)
    signals = load_shadow_signals(
        shadow,
        cfg=cfg,
        weights=weights,
        archive_tapes=archive_tapes,
        reward_multiple=reward_multiple,
    )

    wins = sum(1 for s in signals if s.outcome == "true_win")
    losses = sum(1 for s in signals if s.outcome == "true_loss")
    unresolved = sum(1 for s in signals if s.outcome == "unresolved")

    best, top_candidates, sweep_meta = sweep_threshold_matrix(signals, bases)
    elapsed = time.perf_counter() - t0

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed, 3),
        "archive_path": str(archive),
        "shadow_log_path": str(shadow),
        "signals_evaluated": len(signals),
        "outcome_counts": {
            "true_win": wins,
            "true_loss": losses,
            "unresolved": unresolved,
        },
        "baseline_floors": asdict(bases),
        "sweep_grid": {
            "steps_per_axis": SWEEP_STEPS,
            "config_local_range_pct": "±5.0%",
            "config_local_step_pct": "0.1% of base floor",
            "fallback": "P5–P95 archive-matched metric expansion when config-local finds no zero-loss set",
        },
        "sweep_meta": sweep_meta,
        "best_candidate": asdict(best) if best else None,
        "top_candidates": [asdict(c) for c in top_candidates],
    }

    promotion: dict[str, Any] | None = None
    if best is not None and promote:
        promotion = promote_floors(best, bases=bases, dry_run=dry_run)
        report["promotion"] = promotion
    elif best is None:
        report["promotion_error"] = (
            "No zero-loss threshold combination found — floors not promoted"
        )

    _report_path().parent.mkdir(parents=True, exist_ok=True)
    _report_path().write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline combinatorial matrix backtuner (shadow_log × 5-day archive)"
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Override production_5day_archive.jsonl path",
    )
    parser.add_argument(
        "--shadow-log",
        type=Path,
        default=None,
        help="Override shadow_log.jsonl path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute optimal floors but do not write config/checkpoint",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="Analyze only — skip config_v29 / warmed-alpha promotion",
    )
    args = parser.parse_args(argv)

    report = run_matrix_backtuner(
        archive_path=args.archive,
        shadow_path=args.shadow_log,
        promote=not args.no_promote,
        dry_run=args.dry_run,
    )
    best = report.get("best_candidate")
    print(json.dumps(report, indent=2))
    if best:
        print(
            "\nSWEET SPOT: "
            f"conf≥{best['signal_confidence_floor_pct']}% "
            f"fitness≥{best['environment_fitness_floor_pct']}% "
            f"ml≥{best['ml_veto_floor_probability']} "
            f"→ {best['true_wins_included']} True Wins / "
            f"{best['true_losses_included']} True Losses "
            f"({best['signals_selected']} signals)"
        )
    else:
        print("\nNo zero-loss sweet spot found.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
