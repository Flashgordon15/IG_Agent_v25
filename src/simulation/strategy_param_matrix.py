"""
Strategy parameter isolation matrix — programmatic grid dimensions for walk-forward search.

Replaces hardcoded trial-and-error tuning with named parameter vectors that merge
into the config overlay chain (see config_loader + matrix_search).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

# Walk-forward calibration bounds (Phase 2 — tightened around prior winner).
CALIBRATION_PARAM_MATRIX: dict[str, dict[str, Any]] = {
    "signal_threshold": {
        "label": "Signal Confidence Floor",
        "grid": list(range(50, 66, 1)),
    },
    "reward_multiple": {
        "label": "Take-Profit / Stop-Loss R:R",
        "grid": [round(1.2 + i * 0.1, 1) for i in range(7)],  # 1.2 .. 1.8
    },
    "rsi_buy_min": {
        "label": "RSI Buy Minimum",
        "grid": list(range(62, 67, 1)),
    },
    "rsi_sell_max": {
        "label": "RSI Sell Maximum",
        "grid": list(range(40, 45, 1)),
    },
    "momentum_gap_points": {
        "label": "Momentum Lookback Gap (pts)",
        "grid": list(range(10, 16, 1)),
    },
    "atr_volatility_multiplier": {
        "label": "ATR Volatility Multiplier",
        "grid": [1.0],
    },
    "risk_points": {
        "label": "Stop-Loss Distance (pts proxy)",
        "grid": [40.0],
    },
}

# Authoritative grid axes (Phase 1 — dictionary array source of truth).
STRATEGY_PARAM_MATRIX: dict[str, dict[str, Any]] = {
    "signal_threshold": {
        "label": "Signal Confidence Floor",
        "default": 62.0,
        "grid": list(range(35, 86, 2)),
    },
    "rsi_buy_min": {
        "label": "RSI Buy Minimum",
        "default": 58.0,
        "grid": list(range(50, 71, 2)),
    },
    "rsi_sell_max": {
        "label": "RSI Sell Maximum",
        "default": 45.0,
        "grid": list(range(30, 51, 2)),
    },
    "reward_multiple": {
        "label": "Take-Profit / Stop-Loss R:R",
        "default": 3.0,
        "grid": [1.5, 2.0, 2.5, 3.0, 3.5],
    },
    "momentum_gap_points": {
        "label": "Momentum Lookback Gap (pts)",
        "default": 20.0,
        "grid": list(range(10, 31, 2)),
    },
    "atr_volatility_multiplier": {
        "label": "ATR Volatility Multiplier",
        "default": 1.0,
        "grid": [0.8, 1.0, 1.2, 1.4],
    },
    "risk_points": {
        "label": "Stop-Loss Distance (pts proxy)",
        "default": 40.0,
        "grid": [30.0, 40.0, 50.0],
    },
}


@dataclass(frozen=True)
class ParamVector:
    """Single point in the strategy parameter hypercube."""

    signal_threshold: float = 62.0
    rsi_buy_min: float = 58.0
    rsi_sell_max: float = 45.0
    reward_multiple: float = 3.0
    momentum_gap_points: float = 20.0
    atr_volatility_multiplier: float = 1.0
    risk_points: float = 40.0

    def to_overlay(self) -> dict[str, Any]:
        return {
            "signal_threshold": self.signal_threshold,
            "rsi_buy_min": int(self.rsi_buy_min),
            "rsi_sell_max": int(self.rsi_sell_max),
            "reward_multiple": round(self.reward_multiple, 2),
            "momentum_gap_points": int(self.momentum_gap_points),
            "atr_volatility_multiplier": round(self.atr_volatility_multiplier, 2),
            "risk_points": round(self.risk_points, 1),
            "protective_learning": {
                "signal_threshold_floor": round(self.signal_threshold, 1),
            },
            "matrix_search": {
                "source": "strategy_param_matrix",
                "vector": asdict(self),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParamVector:
        return cls(
            signal_threshold=float(data.get("signal_threshold", 62)),
            rsi_buy_min=float(data.get("rsi_buy_min", 58)),
            rsi_sell_max=float(data.get("rsi_sell_max", 45)),
            reward_multiple=float(data.get("reward_multiple", 3.0)),
            momentum_gap_points=float(data.get("momentum_gap_points", 20)),
            atr_volatility_multiplier=float(
                data.get("atr_volatility_multiplier", 1.0)
            ),
            risk_points=float(data.get("risk_points", 40)),
        )


def default_param_vector() -> ParamVector:
    return ParamVector(
        signal_threshold=float(STRATEGY_PARAM_MATRIX["signal_threshold"]["default"]),
        rsi_buy_min=float(STRATEGY_PARAM_MATRIX["rsi_buy_min"]["default"]),
        rsi_sell_max=float(STRATEGY_PARAM_MATRIX["rsi_sell_max"]["default"]),
        reward_multiple=float(STRATEGY_PARAM_MATRIX["reward_multiple"]["default"]),
        momentum_gap_points=float(STRATEGY_PARAM_MATRIX["momentum_gap_points"]["default"]),
        atr_volatility_multiplier=float(
            STRATEGY_PARAM_MATRIX["atr_volatility_multiplier"]["default"]
        ),
        risk_points=float(STRATEGY_PARAM_MATRIX["risk_points"]["default"]),
    )


def iter_grid(
    *,
    max_permutations: int | None = None,
    reduced: bool = False,
    calibration: bool = False,
) -> Iterator[ParamVector]:
    """Cartesian product over STRATEGY_PARAM_MATRIX grid axes."""
    import itertools

    if calibration:
        axes = {k: v["grid"] for k, v in CALIBRATION_PARAM_MATRIX.items()}
    elif reduced:
        axes = {
            "signal_threshold": [35, 45, 55, 65, 75, 85],
            "rsi_buy_min": list(range(52, 69, 4)),
            "rsi_sell_max": list(range(34, 49, 4)),
            "reward_multiple": [1.5, 2.0, 2.5, 3.0, 3.5],
            "momentum_gap_points": [12, 20, 28],
            "atr_volatility_multiplier": [1.0, 1.2],
            "risk_points": [40.0],
        }
    else:
        axes = {k: v["grid"] for k, v in STRATEGY_PARAM_MATRIX.items()}

    keys = list(axes.keys())
    count = 0
    for combo in itertools.product(*(axes[k] for k in keys)):
        vec = dict(zip(keys, combo))
        yield ParamVector.from_dict(vec)
        count += 1
        if max_permutations is not None and count >= max_permutations:
            break


def grid_size(*, reduced: bool = False, calibration: bool = False) -> int:
    import itertools

    if calibration:
        axes = {k: v["grid"] for k, v in CALIBRATION_PARAM_MATRIX.items()}
    elif reduced:
        axes = {
            "signal_threshold": [35, 45, 55, 65, 75, 85],
            "rsi_buy_min": list(range(52, 69, 4)),
            "rsi_sell_max": list(range(34, 49, 4)),
            "reward_multiple": [1.5, 2.0, 2.5, 3.0, 3.5],
            "momentum_gap_points": [12, 20, 28],
            "atr_volatility_multiplier": [1.0, 1.2],
            "risk_points": [40.0],
        }
    else:
        axes = {k: v["grid"] for k, v in STRATEGY_PARAM_MATRIX.items()}
    n = 1
    for vals in axes.values():
        n *= len(vals)
    return n
