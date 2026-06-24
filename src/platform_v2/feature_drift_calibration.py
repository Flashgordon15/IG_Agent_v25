"""
Adaptive Real-Time Feature Drift Calibration — Platform V2.

Re-normalizes live tick feature vectors when they drift >2σ from training matrix weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from platform_v2 import platform_v2_settings


@dataclass(frozen=True)
class DriftCalibrationResult:
    rsi: float
    atr: float
    momentum: float
    drifted: bool
    max_z: float
    scale_multiplier: float
    details: dict[str, float]


def _settings() -> dict[str, Any]:
    base = platform_v2_settings()
    block = base.get("feature_drift")
    return dict(block) if isinstance(block, dict) else {}


def _training_feature_stats(
    epic: str,
    matrix: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Mean/std of (rsi_anchor, atr_anchor) from populated epic cells."""
    try:
        from intelligence.matrix_prebaker import (
            COL_ATR_ANCHOR,
            COL_RSI_ANCHOR,
            COL_SAMPLES,
            CELLS_PER_EPIC,
            epic_slot,
        )
    except Exception:
        return np.array([50.0, 1.5]), np.array([10.0, 0.5]), 0

    if matrix is None:
        try:
            from intelligence.matrix_prebaker import get_alpha_matrix_segment

            segment = get_alpha_matrix_segment(create=False)
            matrix = segment.matrix
        except Exception:
            return np.array([50.0, 1.5]), np.array([10.0, 0.5]), 0

    slot = epic_slot(epic)
    start = slot * CELLS_PER_EPIC
    end = start + CELLS_PER_EPIC
    rows = matrix[start:end]
    mask = rows[:, COL_SAMPLES] > 0.0
    if not np.any(mask):
        return np.array([50.0, 1.5]), np.array([10.0, 0.5]), 0

    feats = np.column_stack(
        (
            rows[mask, COL_RSI_ANCHOR],
            rows[mask, COL_ATR_ANCHOR],
        )
    ).astype(np.float64)
    mean = np.nanmean(feats, axis=0)
    std = np.nanstd(feats, axis=0)
    std = np.where(std < 1e-6, np.array([10.0, 0.25]), std)
    return mean, std, int(feats.shape[0])


def calibrate_live_features(
    *,
    epic: str,
    rsi: float,
    atr: float,
    momentum: float,
    matrix: np.ndarray | None = None,
) -> DriftCalibrationResult:
    """
    Apply dynamic scaling when live vector drifts beyond sigma threshold.
    """
    cfg = _settings()
    sigma_threshold = float(cfg.get("sigma_threshold", 2.0))
    mean, std, n_cells = _training_feature_stats(epic, matrix=matrix)

    live = np.array([float(rsi), float(atr)], dtype=np.float64)
    z = np.abs((live - mean) / std)
    max_z = float(np.max(z)) if z.size else 0.0
    drifted = max_z > sigma_threshold

    if not drifted:
        return DriftCalibrationResult(
            rsi=float(rsi),
            atr=float(atr),
            momentum=float(momentum),
            drifted=False,
            max_z=round(max_z, 4),
            scale_multiplier=1.0,
            details={"cells": float(n_cells)},
        )

    pull = min(1.0, (max_z - sigma_threshold) / max(sigma_threshold, 0.5))
    scale_mult = 1.0 - pull * float(cfg.get("max_pull_fraction", 0.35))
    adjusted = mean + (live - mean) * scale_mult
    mom_scale = scale_mult if abs(float(momentum)) > 1e-9 else 1.0

    return DriftCalibrationResult(
        rsi=round(float(adjusted[0]), 4),
        atr=round(max(float(adjusted[1]), 0.01), 4),
        momentum=round(float(momentum) * mom_scale, 4),
        drifted=True,
        max_z=round(max_z, 4),
        scale_multiplier=round(scale_mult, 4),
        details={
            "cells": float(n_cells),
            "z_rsi": round(float(z[0]), 4),
            "z_atr": round(float(z[1]), 4),
        },
    )
