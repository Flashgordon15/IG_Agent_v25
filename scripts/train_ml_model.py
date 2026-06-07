#!/usr/bin/env python3
"""Train XGBoost from training_dataset.csv and write model metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.paths import data_dir


def main() -> int:
    dataset = data_dir() / "training_dataset.csv"
    if not dataset.is_file():
        print(
            f"Missing {dataset} — run build_training_dataset.py first", file=sys.stderr
        )
        return 1

    import pandas as pd

    df = pd.read_csv(dataset)
    fired = df[
        df.get("fired", pd.Series(dtype=object))
        .astype(str)
        .str.lower()
        .isin(["true", "1"])
    ]
    label_col = next(
        (c for c in ("label", "label_3bar", "label_3") if c in df.columns),
        None,
    )
    labelled = 0
    if label_col:
        labelled = int(fired[label_col].isin(["WIN", "LOSS"]).sum())

    from trading.ml_scorer import get_ml_scorer

    scorer = get_ml_scorer()
    scorer.train(dataset)
    meta = {
        "dataset_rows": int(len(df)),
        "fired_rows": int(len(fired)),
        "labelled_rows": labelled,
        "features": scorer.feature_names,
    }
    meta_path = data_dir() / "ml_model" / "training_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(
        f"Trained on {labelled} labelled fired rows ({len(fired)} fired, {len(df)} total)"
    )
    print(f"Meta: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
