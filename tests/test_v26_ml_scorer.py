"""Tests for v26 per-epic S4 ML scorer."""

from __future__ import annotations

import json
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.v26_ml_scorer import V26MLScorer, reset_v26_ml_scorer_for_tests


class V26MLScorerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_v26_ml_scorer_for_tests()

    def test_predict_loads_manifest_model(self) -> None:
        try:
            from xgboost import XGBClassifier
        except ImportError:
            self.skipTest("xgboost not installed")
        except Exception as exc:
            if "XGBoost" in type(exc).__name__ or "libomp" in str(exc).lower():
                self.skipTest(f"xgboost runtime unavailable: {exc}")
            raise
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            epic = "IX.D.NIKKEI.IFM.IP"
            slug = "IX_D_NIKKEI_IFM_IP"
            epic_dir = root / "data_lake" / "models" / "s4" / "v1" / slug
            epic_dir.mkdir(parents=True)
            model = XGBClassifier(n_estimators=5, max_depth=2)
            X = pd.DataFrame([{"adjusted_score": 80, "rsi": 50, "atr_ratio": 1.0}] * 40)
            y = [1, 0] * 20
            model.fit(X, y)
            with open(epic_dir / "model.pkl", "wb") as f:
                pickle.dump(model, f)
            rel = f"data_lake/models/s4/v1/{slug}/model.pkl"
            manifest = {
                "version": "v1",
                "by_epic": {
                    epic: {
                        "ok": True,
                        "model_path": rel,
                        "features": ["adjusted_score", "rsi", "atr_ratio"],
                        "veto_eligible": True,
                        "recommended_min_prob": 0.58,
                        "val_wr": 0.55,
                    }
                },
            }
            (root / "data_lake" / "models" / "s4").mkdir(parents=True, exist_ok=True)
            (root / "data_lake" / "models" / "s4" / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with patch("trading.v26_ml_scorer.project_root", return_value=root):
                scorer = V26MLScorer()
            self.assertTrue(scorer.is_eligible(epic))
            prob = scorer.predict(
                epic,
                {"adjusted_score": 85, "rsi": 45, "atr_ratio": 1.2},
            )
            self.assertIsNotNone(prob)
            self.assertGreaterEqual(prob, 0.0)
            self.assertLessEqual(prob, 1.0)


if __name__ == "__main__":
    unittest.main()
