"""
Hierarchical probability engine — deterministic technical filters + Pillar 4 ML brain.

Technical setups crossing 42% ingest a 128-dim state matrix; ML steers promote/veto thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from signals.indicators import STRATEGY_THRESHOLD_LOW_PCT
from signals.signal_engine import SignalResult
from system.engine_log import log_engine

WIN_PROMOTE_FLOOR = 0.65
WIN_VETO_FLOOR = 0.40
PROMOTE_THRESHOLD_RELIEF_PCT = 10.0


@dataclass(frozen=True)
class ProbabilityVerdict:
    win_probability: float
    model_verdict: str
    veto: bool
    promote: bool
    threshold_relief: float
    ml_veto_token: str = ""


def _extract_ml_features(sig: SignalResult, vector: np.ndarray) -> dict[str, float]:
    snap = sig.snapshot or {}
    last = snap.get("last") or {}
    stop = 1.0
    try:
        from system.config_loader import get_config

        stop = max(1.0, float(get_config().stop_distance_points))
    except Exception:
        pass
    atr = float(last.get("atr", 0) or 0)
    return {
        "adjusted_score": float(snap.get("adjusted_confidence") or vector[7] * 100.0),
        "raw_score": float(snap.get("raw_confidence") or vector[8] * 100.0),
        "rsi": float(last.get("rsi", 0) or vector[0] * 100.0),
        "atr_ratio": atr / stop if stop > 0 else 0.0,
    }


def compute_win_probability(
    *,
    sig: SignalResult,
    feature_payload: dict[str, Any],
    epic: str = "",
    market: str = "",
) -> float:
    """Blend Pillar 4 ML scorer with online continuous-optimization weights."""
    vector = np.asarray(feature_payload.get("vector"), dtype=np.float64)
    if vector.size != 128:
        vector = np.zeros(128, dtype=np.float64)

    ml_prob = 0.5
    try:
        from trading.ml_scorer import get_ml_scorer

        scorer = get_ml_scorer()
        if scorer.is_trained():
            feats = _extract_ml_features(sig, vector)
            if all(k in feats for k in scorer.feature_names):
                ml_prob = float(scorer.predict(feats))
    except Exception as exc:
        log_engine(f"probability_engine ml_scorer: {type(exc).__name__}: {exc}")

    opt_prob = 0.5
    try:
        from trading.continuous_optimization_worker import get_continuous_optimization_worker

        opt_prob = float(get_continuous_optimization_worker().predict(vector))
    except Exception as exc:
        log_engine(f"probability_engine continuous opt: {type(exc).__name__}: {exc}")

    if ml_prob != 0.5 or opt_prob != 0.5:
        blended = 0.55 * ml_prob + 0.45 * opt_prob
    else:
        # Heuristic from technical vector when models cold
        directional = float(vector[5] - vector[6])
        rsi_bias = float(vector[0] - 0.5)
        blended = 0.5 + 0.25 * directional + 0.15 * rsi_bias
    return float(max(0.0, min(1.0, blended)))


def apply_hierarchical_probability_gate(
    *,
    sig: SignalResult,
    feature_payload: dict[str, Any],
    peak_score: float,
    threshold: float,
    epic: str = "",
    market: str = "",
) -> ProbabilityVerdict:
    """
    Run ML selection brain when technical setup clears 42% ingestion floor.
    """
    if float(peak_score) < STRATEGY_THRESHOLD_LOW_PCT:
        return ProbabilityVerdict(
            win_probability=0.5,
            model_verdict="NEUTRAL",
            veto=False,
            promote=False,
            threshold_relief=0.0,
        )

    raw_dir = str((sig.snapshot or {}).get("raw_signal") or sig.signal or "").strip()
    if raw_dir not in ("BUY", "SELL"):
        return ProbabilityVerdict(
            win_probability=0.5,
            model_verdict="NEUTRAL",
            veto=False,
            promote=False,
            threshold_relief=0.0,
        )

    win_probability = compute_win_probability(
        sig=sig,
        feature_payload=feature_payload,
        epic=epic,
        market=market,
    )

    if win_probability >= WIN_PROMOTE_FLOOR:
        return ProbabilityVerdict(
            win_probability=win_probability,
            model_verdict="PROMOTE",
            veto=False,
            promote=True,
            threshold_relief=PROMOTE_THRESHOLD_RELIEF_PCT,
        )

    if win_probability < WIN_VETO_FLOOR:
        return ProbabilityVerdict(
            win_probability=win_probability,
            model_verdict="ML_VETO_REJECTION",
            veto=True,
            promote=False,
            threshold_relief=0.0,
            ml_veto_token="ML_VETO_REJECTION",
        )

    return ProbabilityVerdict(
        win_probability=win_probability,
        model_verdict="NEUTRAL",
        veto=False,
        promote=False,
        threshold_relief=0.0,
    )


def annotate_signal_with_probability(
    sig: SignalResult,
    verdict: ProbabilityVerdict,
    feature_payload: dict[str, Any],
) -> SignalResult:
    """Attach probability metadata to signal snapshot for downstream gates."""
    snap = dict(sig.snapshot or {})
    snap["win_probability"] = verdict.win_probability
    snap["model_verdict"] = verdict.model_verdict
    snap["feature_state_ts_ms"] = feature_payload.get("ts_ms")
    snap["feature_state_dim"] = feature_payload.get("dim")
    if verdict.ml_veto_token:
        snap["ml_veto_token"] = verdict.ml_veto_token
    return SignalResult(
        signal=sig.signal,
        raw_confidence=float(sig.raw_confidence),
        adjusted_confidence=float(sig.adjusted_confidence),
        learning_delta=float(sig.learning_delta),
        setup_key=str(sig.setup_key),
        notes=str(sig.notes),
        snapshot=snap,
    )
