"""Background auto-train XGBoost from ml_training_store.jsonl when enough labels exist."""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any

from system.config import Config
from system.engine_log import log_engine

_lock = threading.RLock()
_inflight = False
_last_train_at = 0.0
_last_train_count = 0


def _ml_learning_cfg(cfg: Config | None) -> dict[str, Any]:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return {}
    block = cfg.get("ml_learning") if cfg is not None else None
    return dict(block) if isinstance(block, dict) else {}


def count_win_loss_labels(path: Path | None = None) -> int:
    from data.ml_training_store import default_store_path

    p = path or default_store_path()
    if not p.is_file():
        return 0
    count = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("result") or "").upper() in ("WIN", "LOSS"):
                count += 1
    except Exception:
        return 0
    return count


def _session_slot_idx(slot_id: str | None) -> float | None:
    """Encode BST intraday slot id as ordinal for optional ML features."""
    if not slot_id:
        return None
    from runtime.intraday_slot_tracker import _slots_cfg
    from system.config_loader import get_config

    try:
        cfg = get_config()
    except Exception:
        cfg = None
    for idx, slot in enumerate(_slots_cfg(cfg)):
        if str(slot.get("id") or "") == str(slot_id):
            return float(idx)
    return None


def export_jsonl_training_csv(out_path: Path) -> int:
    """Export WIN/LOSS rows from ML store to CSV for MLScorer.train."""
    from data.ml_training_store import default_store_path

    src = default_store_path()
    rows: list[dict[str, Any]] = []
    if not src.is_file():
        return 0
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = str(row.get("result") or "").upper()
        if result not in ("WIN", "LOSS"):
            continue
        stop = float(row.get("stop_pts") or 4.0)
        atr = float(row.get("atr") or 0.0)
        spread = float(row.get("spread") or 0.0)
        row["label"] = result
        row["atr_ratio"] = atr / max(1.0, stop)
        row["spread_ratio"] = spread / max(1.0, stop)
        row["fired"] = 1
        tier = row.get("profit_tier_pct")
        if tier is not None:
            try:
                row["profit_tier_pct"] = float(tier)
            except (TypeError, ValueError):
                pass
        slot_raw = row.get("session_slot")
        slot_idx = _session_slot_idx(str(slot_raw) if slot_raw else None)
        if slot_idx is not None:
            row["session_slot_idx"] = slot_idx
        rows.append(row)
    if not rows:
        return 0

    fieldnames = sorted({k for r in rows for k in r})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return len(rows)


def train_model_from_store(cfg: Config | None = None) -> dict[str, Any]:
    """Synchronous train — returns status dict."""
    from system.paths import data_dir
    from trading.ml_scorer import get_ml_scorer, reload_ml_scorer

    ml = _ml_learning_cfg(cfg)
    min_labels = int(ml.get("auto_train_min_labels", 30))
    labels = count_win_loss_labels()
    if labels < min_labels:
        return {
            "ok": False,
            "reason": f"insufficient_labels ({labels}<{min_labels})",
            "labels": labels,
        }

    csv_path = data_dir() / "ml_auto_train.csv"
    exported = export_jsonl_training_csv(csv_path)
    if exported < min_labels:
        return {
            "ok": False,
            "reason": f"export_short ({exported}<{min_labels})",
            "labels": exported,
        }

    try:
        scorer = get_ml_scorer()
        scorer.train(csv_path)
        reload_ml_scorer()
        log_engine(
            f"[ML AUTO-TRAIN] model trained on {exported} WIN/LOSS labels "
            f"features={scorer.feature_names}"
        )
        try:
            from runtime.strategy_improvement_tracker import note_ml_model_trained

            note_ml_model_trained()
        except Exception:
            pass
        return {
            "ok": True,
            "labels": exported,
            "features": list(scorer.feature_names),
            "trained": reload_ml_scorer().is_trained(),
        }
    except Exception as exc:
        log_engine(f"[ML AUTO-TRAIN] failed: {type(exc).__name__}: {exc}")
        return {"ok": False, "reason": str(exc), "labels": exported}


def maybe_auto_train_after_close(
    prev_count: int,
    new_count: int,
    cfg: Config | None = None,
) -> None:
    """Schedule background retrain when label milestones hit."""
    global _inflight, _last_train_at, _last_train_count

    ml = _ml_learning_cfg(cfg)
    if not ml.get("auto_train_enabled", True):
        return

    min_labels = int(ml.get("auto_train_min_labels", 30))
    retrain_every = int(ml.get("auto_train_every_n_labels", 10))
    cooldown_sec = float(ml.get("auto_train_cooldown_sec", 300.0))

    labels = count_win_loss_labels()
    if labels < min_labels:
        return

    now = time.time()
    from trading.ml_scorer import get_ml_scorer

    milestone = (
        labels >= min_labels
        and (
            _last_train_count == 0
            or labels - _last_train_count >= retrain_every
            or not get_ml_scorer().is_trained()
        )
        and (now - _last_train_at) >= cooldown_sec
    )
    if not milestone:
        return

    with _lock:
        if _inflight:
            return
        _inflight = True

    def _worker() -> None:
        global _inflight, _last_train_at, _last_train_count
        try:
            result = train_model_from_store(cfg)
            if result.get("ok"):
                _last_train_at = time.time()
                _last_train_count = int(result.get("labels") or labels)
                try:
                    from ml.setup_memory import invalidate_setup_memory_cache

                    invalidate_setup_memory_cache()
                except Exception:
                    pass
        finally:
            with _lock:
                _inflight = False

    threading.Thread(target=_worker, name="ml-auto-train", daemon=True).start()


def reset_auto_trainer_for_tests() -> None:
    global _inflight, _last_train_at, _last_train_count
    with _lock:
        _inflight = False
        _last_train_at = 0.0
        _last_train_count = 0
