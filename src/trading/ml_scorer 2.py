"""ML scorer stub — XGBoost when available; default off via USE_ML_SIGNAL."""

from __future__ import annotations

import json
import pickle
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
        label_col = "label" if "label" in df.columns else df.columns[-1]
        y = df[label_col]
        X = df.drop(columns=[label_col])
        self._feature_names = list(X.columns)
        model = XGBClassifier(
            n_estimators=80,
            max_depth=4,
            learning_rate=0.08,
            eval_metric="logloss",
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
        log_engine(f"ml_scorer trained on {len(df)} rows, {len(self._feature_names)} features")

    def predict(self, features: dict[str, float]) -> float:
        if self._model is None:
            return 0.5
        try:
            import pandas as pd

            row = {k: float(features.get(k, 0.0)) for k in self._feature_names}
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
        timeout_s: float = 0.05,
    ) -> float:
        """Return ML probability in [0, 1]; 0 when disabled, untrained, timed out, or on error."""
        try:
            if not use_ml_signal:
                return 0.0
            if self._model is None:
                return 0.0
            feats = dict(features or {})
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(self.predict, feats)
                try:
                    prob = float(fut.result(timeout=timeout_s))
                except FuturesTimeoutError:
                    log_engine(f"ml_scorer score timed out after {timeout_s}s")
                    return 0.0
            return max(0.0, min(1.0, prob))
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
