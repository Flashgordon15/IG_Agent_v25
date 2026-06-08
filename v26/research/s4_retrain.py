"""
S4_ml_meta — offline per-epic XGBoost retrain from historic labels.

Sources: replay_results.jsonl (fired signals) + ml_training_store.jsonl (live closes).
Writes versioned artifacts under data_lake/models/s4/{version}/{epic_slug}/.
"""

from __future__ import annotations

import json
import pickle
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.trade_learning import load_ml_training_records, load_replay_rows


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_v26_config() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def s4_settings() -> dict[str, Any]:
    block = _load_v26_config().get("s4_ml_meta") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "min_decided_rows": int(block.get("min_decided_rows") or 30),
        "min_val_wr": float(block.get("min_val_wr") or 0.52),
        "val_holdout_pct": float(block.get("val_holdout_pct") or 0.2),
        "models_root": str(block.get("models_dir") or "data_lake/models/s4"),
    }


def _epic_slug(epic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", epic).strip("_") or "unknown"


def _label_binary(row: dict[str, Any]) -> int | None:
    from research.label_utils import outcome_label

    lab = outcome_label(row)
    if lab == "WIN":
        return 1
    if lab == "LOSS":
        return 0
    pnl = row.get("gbp_pnl")
    if pnl is not None:
        try:
            p = float(pnl)
        except (TypeError, ValueError):
            p = 0.0
        if p > 0:
            return 1
        if p < 0:
            return 0
    return None


def _feature_row(
    row: dict[str, Any], *, stop_pts: float = 45.0
) -> dict[str, float] | None:
    try:
        adj = float(row.get("adjusted_score") or row.get("confidence") or 0)
        rsi = float(row.get("rsi") or 0)
        atr = float(row.get("atr") or 0)
    except (TypeError, ValueError):
        return None
    stop = max(1.0, float(row.get("stop_pts") or stop_pts))
    if "atr_ratio" in row:
        try:
            atr_ratio = float(row["atr_ratio"])
        except (TypeError, ValueError):
            atr_ratio = atr / stop
    else:
        atr_ratio = atr / stop
    return {
        "adjusted_score": adj,
        "rsi": rsi,
        "atr_ratio": atr_ratio,
    }


def _instrument_epic_map() -> dict[str, str]:
    import sys

    src = _project_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import json as _json

        from trading.instrument_registry import InstrumentRegistry

        cfg_path = _project_root() / "config" / "config_v25.json"
        if not cfg_path.is_file():
            return {}
        raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
        reg = InstrumentRegistry(raw)
        out: dict[str, str] = {}
        for _iid, inst in reg.get_enabled_with_ids():
            epic = str(inst.get("epic") or "")
            name = str(inst.get("name") or "")
            if epic:
                out[name.lower()] = epic
                out[_iid.lower()] = epic
        return out
    except Exception:
        return {}


def _resolve_epic(row: dict[str, Any], name_map: dict[str, str]) -> str:
    epic = str(row.get("epic") or "").strip()
    if epic:
        return epic
    inst = str(row.get("instrument") or "").strip().lower()
    return name_map.get(inst, inst or "unknown")


def build_s4_rows() -> list[dict[str, Any]]:
    name_map = _instrument_epic_map()
    rows: list[dict[str, Any]] = []
    for row in load_replay_rows():
        if not row.get("fired"):
            continue
        y = _label_binary(row)
        if y is None:
            continue
        feats = _feature_row(row, stop_pts=float(row.get("stop_pts") or 45))
        if feats is None:
            continue
        rows.append(
            {
                "source": "replay",
                "epic": str(row.get("epic") or ""),
                "timestamp": str(row.get("timestamp") or ""),
                "y": y,
                **feats,
            }
        )
    for row in load_ml_training_records():
        y = _label_binary(row)
        if y is None:
            continue
        feats = _feature_row(
            {
                "adjusted_score": row.get("confidence"),
                "rsi": row.get("rsi"),
                "atr": row.get("atr"),
                "stop_pts": 45,
            }
        )
        if feats is None:
            continue
        rows.append(
            {
                "source": "ml_store",
                "epic": _resolve_epic(row, name_map),
                "timestamp": str(row.get("exit_time") or row.get("entry_time") or ""),
                "y": y,
                **feats,
            }
        )
    return rows


def _train_epic_model(
    epic_rows: list[dict[str, Any]],
    *,
    min_decided: int,
    min_val_wr: float,
    holdout_pct: float,
) -> dict[str, Any] | None:
    if len(epic_rows) < min_decided:
        return None
    try:
        import pandas as pd
        from xgboost import XGBClassifier
    except ImportError:
        return {"error": "xgboost/pandas not installed"}

    epic_rows = sorted(epic_rows, key=lambda r: str(r.get("timestamp") or ""))
    split = max(1, int(len(epic_rows) * (1.0 - holdout_pct)))
    train_rows = epic_rows[:split]
    val_rows = epic_rows[split:] or epic_rows[-max(1, len(epic_rows) // 5) :]

    feature_cols = ["adjusted_score", "rsi", "atr_ratio"]

    def _to_xy(rows: list[dict[str, Any]]):
        X = pd.DataFrame([{c: float(r[c]) for c in feature_cols} for r in rows])
        y = pd.Series([int(r["y"]) for r in rows])
        return X, y

    X_train, y_train = _to_xy(train_rows)
    X_val, y_val = _to_xy(val_rows)
    model = XGBClassifier(
        n_estimators=80,
        max_depth=4,
        learning_rate=0.08,
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)
    val_pred = model.predict(X_val)
    val_wr = float((val_pred == y_val).mean()) if len(y_val) else 0.0
    wins = int(y_val.sum())
    val_decided = len(y_val)
    eligible = val_wr >= min_val_wr and val_decided >= max(10, min_decided // 3)
    return {
        "model": model,
        "features": feature_cols,
        "train_n": len(train_rows),
        "val_n": val_decided,
        "val_wr": round(val_wr, 4),
        "val_wins": wins,
        "veto_eligible": eligible,
        "recommended_min_prob": round(0.52 + (val_wr - 0.5) * 0.2, 2),
    }


def run_s4_retrain(*, version: str | None = None) -> dict[str, Any]:
    cfg = s4_settings()
    version = version or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_rows = build_s4_rows()
    by_epic: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        epic = str(row.get("epic") or "unknown")
        by_epic.setdefault(epic, []).append(row)

    root = _project_root() / cfg["models_root"] / version
    root.mkdir(parents=True, exist_ok=True)
    epic_manifest: dict[str, Any] = {}

    for epic, epic_rows in sorted(by_epic.items()):
        trained = _train_epic_model(
            epic_rows,
            min_decided=int(cfg["min_decided_rows"]),
            min_val_wr=float(cfg["min_val_wr"]),
            holdout_pct=float(cfg["val_holdout_pct"]),
        )
        if not trained or trained.get("error"):
            epic_manifest[epic] = {
                "ok": False,
                "rows": len(epic_rows),
                "error": trained.get("error") if trained else "insufficient_rows",
            }
            continue
        slug = _epic_slug(epic)
        epic_dir = root / slug
        epic_dir.mkdir(parents=True, exist_ok=True)
        model_path = epic_dir / "model.pkl"
        with open(model_path, "wb") as f:
            pickle.dump(trained["model"], f)
        meta = {
            "epic": epic,
            "version": version,
            "features": trained["features"],
            "train_n": trained["train_n"],
            "val_n": trained["val_n"],
            "val_wr": trained["val_wr"],
            "veto_eligible": trained["veto_eligible"],
            "recommended_min_prob": trained["recommended_min_prob"],
        }
        (epic_dir / "meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        epic_manifest[epic] = {
            "ok": True,
            "model_path": str(model_path.relative_to(_project_root())),
            **meta,
        }

    manifest = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_labelled_rows": len(all_rows),
        "epics_trained": sum(1 for e in epic_manifest.values() if e.get("ok")),
        "epics_veto_eligible": sum(
            1 for e in epic_manifest.values() if e.get("veto_eligible")
        ),
        "by_epic": epic_manifest,
        "s4_enabled_in_config": bool(cfg.get("enabled")),
    }
    manifest_root = _project_root() / cfg["models_root"]
    manifest_root.mkdir(parents=True, exist_ok=True)
    (manifest_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (manifest_root / "active_version.txt").write_text(version, encoding="utf-8")
    return manifest


def write_s4_manifest() -> Path:
    manifest = run_s4_retrain()
    return _project_root() / s4_settings()["models_root"] / "manifest.json"
