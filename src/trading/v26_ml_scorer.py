"""Load per-epic S4 XGBoost models from v26 manifest (offline retrain artifacts)."""

from __future__ import annotations

import json
import pickle
import threading
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import project_root


class V26MLScorer:
    def __init__(self) -> None:
        self._manifest: dict[str, Any] = {}
        self._models: dict[str, Any] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._load()

    def _models_root(self) -> Path:
        return project_root() / "data_lake" / "models" / "s4"

    def _load(self) -> None:
        manifest_path = self._models_root() / "manifest.json"
        if not manifest_path.is_file():
            return
        try:
            self._manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log_engine(f"v26_ml_scorer manifest load failed: {e}")
            return
        for epic, info in (self._manifest.get("by_epic") or {}).items():
            if not info.get("ok"):
                continue
            rel = str(info.get("model_path") or "")
            model_path = project_root() / rel
            if not model_path.is_file():
                continue
            try:
                with open(model_path, "rb") as f:
                    self._models[epic] = pickle.load(f)
                self._meta[epic] = info
            except Exception as e:
                log_engine(f"v26_ml_scorer load {epic}: {type(e).__name__}: {e}")

    def reload(self) -> None:
        self._manifest = {}
        self._models = {}
        self._meta = {}
        self._load()

    def has_manifest(self) -> bool:
        return bool(self._manifest.get("by_epic"))

    def is_eligible(self, epic: str) -> bool:
        info = self._meta.get(epic) or {}
        return bool(info.get("veto_eligible")) and epic in self._models

    def epic_meta(self, epic: str) -> dict[str, Any]:
        return dict(self._meta.get(epic) or {})

    def recommended_min_prob(self, epic: str) -> float:
        info = self._meta.get(epic) or {}
        try:
            return float(info.get("recommended_min_prob") or 0.58)
        except (TypeError, ValueError):
            return 0.58

    def predict(self, epic: str, features: dict[str, float]) -> float | None:
        model = self._models.get(epic)
        if model is None:
            return None
        info = self._meta.get(epic) or {}
        feat_names = list(info.get("features") or [])
        if not feat_names:
            return None
        try:
            import pandas as pd

            missing = [k for k in feat_names if k not in features]
            if missing:
                log_engine(f"v26_ml_scorer {epic}: missing {missing}")
                return None
            row = {k: float(features[k]) for k in feat_names}
            prob = float(model.predict_proba(pd.DataFrame([row]))[0][1])
            return max(0.0, min(1.0, prob))
        except Exception as e:
            log_engine(f"v26_ml_scorer predict {epic}: {type(e).__name__}: {e}")
            return None

    def score(
        self,
        epic: str,
        features: dict[str, float] | None = None,
        *,
        timeout_s: float = 0.5,
    ) -> float | None:
        feats = dict(features or {})
        result: list[float | None] = []
        exc: list[BaseException] = []

        def _run() -> None:
            try:
                result.append(self.predict(epic, feats))
            except Exception as e:
                exc.append(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout_s)
        if exc:
            raise exc[0]
        return result[0] if result else None


_scorer: V26MLScorer | None = None


def get_v26_ml_scorer() -> V26MLScorer:
    global _scorer
    if _scorer is None:
        _scorer = V26MLScorer()
    return _scorer


def reset_v26_ml_scorer_for_tests() -> None:
    global _scorer
    _scorer = None
