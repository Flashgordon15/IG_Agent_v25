"""ML scorer stub — XGBoost when available; default off via USE_ML_SIGNAL."""

from __future__ import annotations

import json
import pickle
import threading
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_MODEL_DIR = data_dir() / "ml_model"
_MODEL_FILE = _MODEL_DIR / "model.pkl"
_META_FILE = _MODEL_DIR / "meta.json"


class MLScorer:
    def __init__(self) -> None:
        self._model: Any = None
        self._feature_names: list[str] = []
        self._load()

    def _load(self) -> None:
        if not _MODEL_FILE.is_file():
            return
        try:
            with open(_MODEL_FILE, "rb") as f:
                self._model = pickle.load(f)
            if _META_FILE.is_file():
                meta = json.loads(_META_FILE.read_text(encoding="utf-8"))
                self._feature_names = list(meta.get("features") or [])
        except Exception as e:
            log_engine(f"ml_scorer load failed: {type(e).__name__}: {e}")
            self._model = None

    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def feature_names(self) -> list[str]:
        return list(self._feature_names)

    def train(self, dataset_path: str | Path) -> None:
        path = Path(dataset_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            import pandas as pd
            from xgboost import XGBClassifier
        except ImportError as e:
            raise RuntimeError("xgboost and pandas required for MLScorer.train") from e

        df = pd.read_csv(path)

        # Select the correct label column — prefer label_3bar over last column
        if "label" in df.columns:
            label_col = "label"
        elif "label_3bar" in df.columns:
            label_col = "label_3bar"
        elif "label_6bar" in df.columns:
            label_col = "label_6bar"
        else:
            label_col = df.columns[-1]

        # Map string labels to binary, dropping BREAKEVEN rows
        label_map = {"WIN": 1, "LOSS": 0, 1: 1, 0: 0, 1.0: 1, 0.0: 0}
        df["_y"] = df[label_col].map(label_map)
        df = df[df["_y"].notna()].copy()
        y = df["_y"].astype(int)

        # Normalise instrument-specific magnitudes so the model generalises across
        # markets with very different price scales (e.g. Wall Street ATR ~50 vs
        # Gold ATR ~3). Express ATR and spread as fractions of the stop distance —
        # a dimensionless ratio that is comparable across all instruments.
        if "atr" in df.columns and "stop_pts" in df.columns:
            safe_stop = df["stop_pts"].clip(lower=1.0)
            df["atr_ratio"] = df["atr"] / safe_stop
            df["spread_ratio"] = df["spread"] / safe_stop if "spread" in df.columns else 0.0
        elif "atr" in df.columns:
            df["atr_ratio"] = df["atr"]
            df["spread_ratio"] = df["spread"] if "spread" in df.columns else 0.0

        # Only keep numeric features that are also available at inference time.
        # spread_ratio and fired are excluded: spread_ratio has near-zero variance
        # in training data (constant spread/stop) and fired=1 for all trained rows,
        # so both are uninformative and cause out-of-distribution issues at inference.
        inference_features = [
            "adjusted_score", "raw_score", "rsi", "atr_ratio",
        ]
        keep = [c for c in inference_features if c in df.columns]
        X = df[keep].copy()
        # fired is bool — coerce to int
        if "fired" in X.columns:
            X["fired"] = X["fired"].astype(int)

        self._feature_names = list(X.columns)
        model = XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.08,
            eval_metric="logloss",
            scale_pos_weight=1,
        )
        model.fit(X, y)
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_FILE, "wb") as f:
            pickle.dump(model, f)
        _META_FILE.write_text(
            json.dumps({"features": self._feature_names}, indent=2),
            encoding="utf-8",
        )
        self._model = model
        log_engine(f"ml_scorer trained on {len(df)} rows ({int(y.sum())} wins), {len(self._feature_names)} features")

    def predict(self, features: dict[str, float]) -> float:
        if self._model is None:
            return 0.5
        try:
            import pandas as pd

            missing = [k for k in self._feature_names if k not in features]
            if missing:
                log_engine(f"ml_scorer predict: missing features {missing} — skipping")
                return 0.5
            row = {k: float(features[k]) for k in self._feature_names}
            X = pd.DataFrame([row])
            prob = float(self._model.predict_proba(X)[0][1])
            return max(0.0, min(1.0, prob))
        except Exception as e:
            log_engine(f"ml_scorer predict failed: {type(e).__name__}: {e}")
            return 0.5

    def score(
        self,
        features: dict[str, float] | None = None,
        *,
        use_ml_signal: bool = False,
        timeout_s: float = 0.5,
    ) -> float:
        """Return ML probability in [0, 1]; 0 when disabled, untrained, timed out, or on error."""
        try:
            if not use_ml_signal:
                return 0.0
            if self._model is None:
                return 0.0
            feats = dict(features or {})
            result: list[float] = []
            exc: list[BaseException] = []

            def _run() -> None:
                try:
                    result.append(self.predict(feats))
                except Exception as e:
                    exc.append(e)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=timeout_s)
            if t.is_alive():
                log_engine(f"ml_scorer score timed out after {timeout_s}s")
                return 0.0
            if exc:
                raise exc[0]
            if not result:
                return 0.0
            return max(0.0, min(1.0, float(result[0])))
        except Exception as e:
            log_engine(f"ml_scorer score failed: {type(e).__name__}: {e}")
            return 0.0

    def save(self) -> None:
        if self._model is None:
            return
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_FILE, "wb") as f:
            pickle.dump(self._model, f)

    def load(self) -> None:
        self._load()


_scorer: MLScorer | None = None


def get_ml_scorer() -> MLScorer:
    global _scorer
    if _scorer is None:
        _scorer = MLScorer()
    return _scorer
