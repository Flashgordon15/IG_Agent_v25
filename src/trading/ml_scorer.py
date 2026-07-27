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

_HOLDOUT_FRACTION = 0.20
_TIMESTAMP_COLUMNS = ("timestamp", "ts", "time", "datetime", "date", "created_at")


def _find_timestamp_column(columns: list[str]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for name in _TIMESTAMP_COLUMNS:
        if name in lowered:
            return lowered[name]
    for c in columns:
        low = c.lower()
        if "time" in low or "date" in low:
            return c
    return None


def _rank_auc(y_true: list[int], y_score: list[float]) -> float | None:
    pos = [s for t, s in zip(y_true, y_score) if t == 1]
    neg = [s for t, s in zip(y_true, y_score) if t == 0]
    if not pos or not neg:
        return None
    ranked = sorted(zip(y_score, y_true))
    ranks: dict[int, float] = {}
    i = 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j
    rank_sum_pos = sum(r for k, r in ranks.items() if ranked[k][1] == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _manual_logloss(y_true: list[int], y_score: list[float]) -> float:
    import math

    eps = 1e-15
    total = 0.0
    for t, s in zip(y_true, y_score):
        p = min(max(s, eps), 1.0 - eps)
        total += -(t * math.log(p) + (1 - t) * math.log(1.0 - p))
    return total / max(1, len(y_true))


def _holdout_metrics(
    y_true: Any, y_score: Any
) -> tuple[float | None, float | None]:
    yt = [int(v) for v in y_true]
    ys = [float(v) for v in y_score]
    try:
        from sklearn.metrics import log_loss, roc_auc_score

        auc = float(roc_auc_score(yt, ys)) if len(set(yt)) > 1 else None
        ll = float(log_loss(yt, ys, labels=[0, 1]))
        return auc, ll
    except ImportError:
        return _rank_auc(yt, ys), _manual_logloss(yt, ys)


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
        except ImportError as e:
            raise RuntimeError("pandas required for MLScorer.train") from e

        df = pd.read_csv(path)

        # Restrict to fired signals only — non-fired rows are analysis-only
        # and should not be used to train the model (they lack a real entry decision).
        if "fired" in df.columns:
            df = df[df["fired"].astype(str).str.lower().isin(["true", "1"])].copy()

        # Select the correct label column — prefer label_3bar / label_3 over last column
        if "label" in df.columns:
            label_col = "label"
        elif "label_3bar" in df.columns:
            label_col = "label_3bar"
        elif "label_3" in df.columns:
            label_col = "label_3"
        elif "label_6bar" in df.columns:
            label_col = "label_6bar"
        elif "label_6" in df.columns:
            label_col = "label_6"
        else:
            label_col = df.columns[-1]

        # Map string labels to binary, dropping BREAKEVEN rows
        label_map = {"WIN": 1, "LOSS": 0, 1: 1, 0: 0}
        df["_y"] = df[label_col].map(label_map)
        df = df[df["_y"].notna()].copy()

        # Chronological order so the holdout is strictly out-of-time
        ts_col = _find_timestamp_column([c for c in df.columns if c != "_y"])
        if ts_col is not None:
            ts = pd.to_datetime(df[ts_col], errors="coerce")
            if ts.notna().any():
                df = df.assign(_ts=ts).sort_values("_ts", kind="stable").drop(columns="_ts")

        y = df["_y"].astype(int)

        # Normalise instrument-specific magnitudes so the model generalises across
        # markets with very different price scales (e.g. Wall Street ATR ~50 vs
        # Gold ATR ~3). Express ATR and spread as fractions of the stop distance —
        # a dimensionless ratio that is comparable across all instruments.
        if "atr" in df.columns and "stop_pts" in df.columns:
            safe_stop = df["stop_pts"].clip(lower=1.0)
            df["atr_ratio"] = df["atr"] / safe_stop
            df["spread_ratio"] = (
                df["spread"] / safe_stop if "spread" in df.columns else 0.0
            )
        elif "atr" in df.columns:
            df["atr_ratio"] = df["atr"]
            df["spread_ratio"] = df["spread"] if "spread" in df.columns else 0.0

        # Core features always used when present; widened OHLC/regime set is
        # included when ≥10% of labelled rows carry the column (legacy 2-feature
        # CSVs keep working). Optional tier/slot follow the same fill gate.
        from ml.replay_features import FEATURE_NAMES as _WIDENED_FEATURES

        core_features = [
            "adjusted_score",
            "raw_score",
            "rsi",
            "atr_ratio",
            "spread_ratio",
        ]
        optional_features = [
            "profit_tier_pct",
            "session_slot_idx",
            "range_ratio",
            "ret_1",
            "ret_3",
            "ret_6",
            "ret_12",
            "momentum_12",
            "vol_regime_idx",
            "session_window_idx",
        ]
        # Prefer the documented widened order when columns exist.
        ordered = [c for c in _WIDENED_FEATURES if c in df.columns]
        keep = [c for c in core_features if c in df.columns]
        for c in ordered:
            if c not in keep and c in df.columns:
                keep.append(c)
        n_rows = max(1, len(df))
        for col in optional_features:
            if col not in df.columns or col in keep:
                continue
            filled = df[col].notna().sum()
            if filled / n_rows >= 0.10:
                keep.append(col)
        X = df[keep].copy()
        X = X.fillna(0.0)
        if "fired" in X.columns:
            X["fired"] = X["fired"].astype(int)

        self._feature_names = list(X.columns)
        holdout_auc: float | None = None
        holdout_logloss: float | None = None
        backend = "xgboost"
        cut = int(len(X) * (1.0 - _HOLDOUT_FRACTION))

        model = self._fit_classifier(X, y, cut=cut)
        if model is None:
            backend = "sklearn"
            model = self._fit_sklearn_classifier(X, y, cut=cut)

        if model is None:
            raise RuntimeError("no ML backend available (install xgboost or scikit-learn)")

        if 0 < cut < len(X):
            y_hold = y.iloc[cut:]
            if y_hold.nunique() > 1 and len(y_hold) > 0:
                try:
                    probs = model.predict_proba(X.iloc[cut:])[:, 1]
                    holdout_auc, holdout_logloss = _holdout_metrics(y_hold, probs)
                except Exception as e:
                    log_engine(
                        f"ml_scorer holdout evaluation failed: {type(e).__name__}: {e}"
                    )

        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_FILE, "wb") as f:
            pickle.dump(model, f)
        _META_FILE.write_text(
            json.dumps(
                {"features": self._feature_names, "backend": backend},
                indent=2,
            ),
            encoding="utf-8",
        )
        self._model = model
        log_engine(
            f"ml_scorer trained ({backend}) on {len(df)} rows ({int(y.sum())} wins), "
            f"{len(self._feature_names)} features | "
            f"holdout_auc={holdout_auc if holdout_auc is None else round(holdout_auc, 4)} "
            f"holdout_logloss={holdout_logloss if holdout_logloss is None else round(holdout_logloss, 4)}"
        )

    def _fit_classifier(self, X: Any, y: Any, *, cut: int) -> Any | None:
        try:
            from xgboost import XGBClassifier
        except Exception as exc:
            log_engine(
                f"ml_scorer: xgboost unavailable ({type(exc).__name__}) — "
                f"trying sklearn fallback"
            )
            return None
        params = dict(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.08,
            eval_metric="logloss",
            scale_pos_weight=1,
        )
        try:
            model = XGBClassifier(**params)
            model.fit(X, y)
            return model
        except Exception as exc:
            log_engine(
                f"ml_scorer xgboost train failed: {type(exc).__name__}: {exc}"
            )
            return None

    def _fit_sklearn_classifier(self, X: Any, y: Any, *, cut: int) -> Any | None:
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
        except ImportError:
            return None
        try:
            pipe = Pipeline(
                [
                    ("scale", StandardScaler()),
                    (
                        "clf",
                        LogisticRegression(
                            max_iter=500,
                            class_weight="balanced",
                        ),
                    ),
                ]
            )
            pipe.fit(X, y)
            return pipe
        except Exception as exc:
            log_engine(
                f"ml_scorer sklearn train failed: {type(exc).__name__}: {exc}"
            )
            return None

    def predict(self, features: dict[str, float]) -> float:
        if self._model is None:
            return 0.5
        try:
            import numpy as np

            missing = [k for k in self._feature_names if k not in features]
            if missing:
                log_engine(f"ml_scorer predict: missing features {missing} — skipping")
                return 0.5
            # Row built in the canonical training feature order (meta.json).
            # xgboost's inplace_predict path validates ndarray inputs by column
            # count only, so a bare 2-D array is safe and avoids the per-call
            # DataFrame construction cost.
            X = np.array(
                [[float(features[k]) for k in self._feature_names]], dtype=np.float32
            )
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


def reload_ml_scorer() -> MLScorer:
    """Reload model.pkl from disk after background auto-train."""
    global _scorer
    _scorer = MLScorer()
    _scorer.load()
    return _scorer
