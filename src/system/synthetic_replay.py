"""
Pillar C — Synthetic Replay Overdrive.

Autonomous background optimizer: reads confirmed ML training closes, replays
10,000 filter permutations against the 24-hour loss buffer, and writes calibrated
filter overrides to src/data/ml_model/meta.json when drawdown setups are found.

Fail-safe: zero losses in the lookback window → informational log only; meta.json
is not modified.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from data.ml_training_store import MLTrainingStore, default_store_path
from system.engine_log import log_engine
from system.ml_filter_overrides import (
    BASELINE_MAX_RSI,
    ML_MIN_TRAINING_RECORDS,
    ML_RAMP_MIN_RECORDS,
)
from system.paths import data_dir

LOOKBACK_HOURS = 24
PERMUTATION_CYCLES = 10_000
BASELINE_FILTER_OVERRIDES = {"max_rsi": BASELINE_MAX_RSI}
_META_PATH = data_dir() / "ml_model" / "meta.json"
_DEFAULT_STOP_PTS = 45.0
_ATR_BUCKET_RE = re.compile(r"atr(\d+)(?:-(\d+))?", re.IGNORECASE)

FEATURE_KEYS = ("adjusted_score", "raw_score", "rsi", "atr_ratio")


@dataclass(frozen=True)
class FeatureSnapshot:
    """Inference-aligned feature vector for permutation replay."""

    adjusted_score: float
    raw_score: float
    rsi: float
    atr_ratio: float
    deal_id: str = ""
    result: str = ""
    instrument: str = ""


@dataclass
class FilterOverrides:
    """Threshold bounds — a trade is blocked when any bound is violated."""

    min_adjusted_score: float | None = None
    max_adjusted_score: float | None = None
    min_raw_score: float | None = None
    max_raw_score: float | None = None
    min_rsi: float | None = None
    max_rsi: float | None = None
    min_atr_ratio: float | None = None
    max_atr_ratio: float | None = None

    def active_bounds(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, value in asdict(self).items():
            if value is not None:
                out[key] = float(value)
        return out


@dataclass
class PermutationResult:
    filters: FilterOverrides
    losses_blocked: int
    loss_total: int
    wins_blocked: int
    win_total: int


def _parse_record_time(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _atr_stop_from_setup(setup_name: str) -> float | None:
    match = _ATR_BUCKET_RE.search(str(setup_name or ""))
    if not match:
        return None
    low = float(match.group(1))
    high = float(match.group(2) or low)
    return max(1.0, (low + high) / 2.0)


def _infer_stop_pts(row: dict[str, Any]) -> float:
    for key in ("stop_pts", "stop_distance", "stop_distance_points"):
        try:
            val = float(row.get(key) or 0)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    parsed = _atr_stop_from_setup(str(row.get("setup_name") or ""))
    if parsed is not None:
        return parsed
    return _DEFAULT_STOP_PTS


def extract_feature_snapshot(row: dict[str, Any]) -> FeatureSnapshot | None:
    """Map ML store row → ml_scorer feature vector with safe fallbacks."""
    try:
        adjusted = float(row.get("adjusted_score") or row.get("confidence") or 0)
        raw = float(row.get("raw_score") or adjusted)
        rsi = float(row.get("rsi") or 0)
        atr = float(row.get("atr") or 0)
    except (TypeError, ValueError):
        return None
    stop = _infer_stop_pts(row)
    if "atr_ratio" in row:
        try:
            atr_ratio = float(row["atr_ratio"])
        except (TypeError, ValueError):
            atr_ratio = atr / max(stop, 1.0)
    else:
        atr_ratio = atr / max(stop, 1.0)
    return FeatureSnapshot(
        adjusted_score=adjusted,
        raw_score=raw,
        rsi=rsi,
        atr_ratio=atr_ratio,
        deal_id=str(row.get("deal_id") or ""),
        result=str(row.get("result") or ""),
        instrument=str(row.get("instrument") or ""),
    )


def is_loss_record(row: dict[str, Any]) -> bool:
    result = str(row.get("result") or "").strip().upper()
    if result == "LOSS":
        return True
    for key in ("gbp_pnl", "pts_pnl", "ig_pnl_currency"):
        try:
            if float(row.get(key) or 0) < 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def iter_store_records(
    path: Path | None = None,
    *,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read JSONL training store rows (optionally filtered by exit/entry time)."""
    store_path = Path(path) if path else default_store_path()
    if not store_path.is_file():
        return []
    cutoff = since.astimezone(timezone.utc) if since else None
    rows: list[dict[str, Any]] = []
    with open(store_path, "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if cutoff is not None:
                ts = _parse_record_time(row.get("exit_time")) or _parse_record_time(
                    row.get("entry_time")
                )
                if ts is None or ts < cutoff:
                    continue
            rows.append(row)
    return rows


def load_loss_snapshots(
    *,
    store_path: Path | None = None,
    lookback_hours: int = LOOKBACK_HOURS,
) -> tuple[list[FeatureSnapshot], list[FeatureSnapshot]]:
    """Return (loss_snapshots, full_history_buffer) for the preceding cycle."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    rows = iter_store_records(store_path, since=since)
    history: list[FeatureSnapshot] = []
    losses: list[FeatureSnapshot] = []
    for row in rows:
        snap = extract_feature_snapshot(row)
        if snap is None:
            continue
        history.append(snap)
        if is_loss_record(row):
            losses.append(snap)
    return losses, history


def would_block(features: FeatureSnapshot, filters: FilterOverrides) -> bool:
    """True when this feature vector would have been soft-blocked by filters."""
    checks: list[tuple[float, float | None, float | None]] = [
        (
            features.adjusted_score,
            filters.min_adjusted_score,
            filters.max_adjusted_score,
        ),
        (features.raw_score, filters.min_raw_score, filters.max_raw_score),
        (features.rsi, filters.min_rsi, filters.max_rsi),
        (features.atr_ratio, filters.min_atr_ratio, filters.max_atr_ratio),
    ]
    for value, lower, upper in checks:
        if lower is not None and value < lower:
            return True
        if upper is not None and value > upper:
            return True
    return False


def _evaluate_filters(
    filters: FilterOverrides,
    losses: list[FeatureSnapshot],
    history: list[FeatureSnapshot],
) -> PermutationResult:
    loss_blocked = sum(1 for snap in losses if would_block(snap, filters))
    wins = [snap for snap in history if snap.result.upper() == "WIN"]
    win_blocked = sum(1 for snap in wins if would_block(snap, filters))
    return PermutationResult(
        filters=filters,
        losses_blocked=loss_blocked,
        loss_total=len(losses),
        wins_blocked=win_blocked,
        win_total=len(wins),
    )


def _sample_filters(
    rng: random.Random, losses: list[FeatureSnapshot]
) -> FilterOverrides:
    """Draw one random filter permutation anchored to loss feature envelopes."""
    if not losses:
        return FilterOverrides()

    adj_vals = [s.adjusted_score for s in losses]
    raw_vals = [s.raw_score for s in losses]
    rsi_vals = [s.rsi for s in losses]
    atr_vals = [s.atr_ratio for s in losses]

    choice = rng.randint(0, 3)
    filters = FilterOverrides()

    if choice == 0:
        filters.max_adjusted_score = rng.uniform(min(adj_vals) - 5.0, min(adj_vals))
    elif choice == 1:
        filters.min_adjusted_score = rng.uniform(max(adj_vals), max(adj_vals) + 5.0)
    elif choice == 2:
        if rng.random() < 0.5:
            filters.max_rsi = rng.uniform(min(rsi_vals) - 2.0, min(rsi_vals))
        else:
            filters.min_rsi = rng.uniform(max(rsi_vals), max(rsi_vals) + 2.0)
    else:
        if rng.random() < 0.5:
            filters.max_atr_ratio = rng.uniform(min(atr_vals) * 0.9, min(atr_vals))
        else:
            filters.min_atr_ratio = rng.uniform(max(atr_vals), max(atr_vals) * 1.1)

    if rng.random() < 0.35:
        filters.max_raw_score = rng.uniform(min(raw_vals) - 3.0, min(raw_vals))
    if rng.random() < 0.25:
        filters.min_raw_score = rng.uniform(max(raw_vals), max(raw_vals) + 3.0)

    return filters


def _envelope_filters(losses: list[FeatureSnapshot]) -> FilterOverrides:
    """Deterministic envelope that blocks every loss in the buffer."""
    adj = [s.adjusted_score for s in losses]
    raw = [s.raw_score for s in losses]
    rsi = [s.rsi for s in losses]
    atr = [s.atr_ratio for s in losses]
    return FilterOverrides(
        max_adjusted_score=min(adj) - 0.01,
        max_raw_score=min(raw) - 0.01,
        max_rsi=min(rsi) - 0.01,
        max_atr_ratio=min(atr) * 0.99,
    )


def run_permutation_sweep(
    losses: list[FeatureSnapshot],
    history: list[FeatureSnapshot],
    *,
    cycles: int = PERMUTATION_CYCLES,
    seed: int = 42,
) -> PermutationResult | None:
    """Run permutation sweep; return best all-loss-blocking filter set."""
    if not losses:
        return None

    rng = random.Random(seed)
    best: PermutationResult | None = None

    envelope = _envelope_filters(losses)
    candidates = [envelope]
    for _ in range(max(0, cycles - 1)):
        candidates.append(_sample_filters(rng, losses))

    for filt in candidates:
        result = _evaluate_filters(filt, losses, history)
        if result.losses_blocked < result.loss_total:
            continue
        if best is None:
            best = result
            continue
        if result.wins_blocked < best.wins_blocked:
            best = result
        elif result.wins_blocked == best.wins_blocked and len(
            result.filters.active_bounds()
        ) < len(best.filters.active_bounds()):
            best = result

    return best


def shadow_training_record_count() -> int:
    """Rows in shadow_training_registry (IG imports isolated from live learning)."""
    try:
        from data.learning_store import LearningStore
        from data.shadow_training_registry import count_rows
        from system.paths import data_dir

        db = data_dir() / "learning_db.sqlite3"
        if not db.is_file():
            return 0
        store = LearningStore(str(db))
        return count_rows(store.conn)
    except Exception:
        return 0


def training_record_count(store: MLTrainingStore) -> int:
    """Effective ML training row count (live store + replay labels + shadow registry)."""
    live = store.record_count()
    shadow = shadow_training_record_count()
    training_meta_path = data_dir() / "ml_model" / "training_meta.json"
    try:
        if training_meta_path.is_file():
            meta = json.loads(training_meta_path.read_text(encoding="utf-8"))
            replay = int(meta.get("labelled_rows") or 0)
            return max(live + shadow, replay)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return live + shadow


def load_meta(path: Path = _META_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {"features": list(FEATURE_KEYS)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"features": list(FEATURE_KEYS)}
    except (json.JSONDecodeError, OSError):
        return {"features": list(FEATURE_KEYS)}


def write_meta(meta: dict[str, Any], path: Path = _META_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def run_synthetic_replay(
    *,
    store_path: Path | None = None,
    meta_path: Path = _META_PATH,
    lookback_hours: int = LOOKBACK_HOURS,
    cycles: int = PERMUTATION_CYCLES,
) -> int:
    """
    Execute one synthetic replay calibration pass.

    Returns process exit code (0 = success / no-op, 1 = error).
    """
    try:
        store = MLTrainingStore(store_path)
        losses, history = load_loss_snapshots(
            store_path=store.path,
            lookback_hours=lookback_hours,
        )
        log_engine(
            f"synthetic_replay: store={store.path.name} "
            f"history={len(history)} losses={len(losses)} "
            f"lookback={lookback_hours}h"
        )

        if not losses:
            log_engine(
                "synthetic_replay: no losses in 24h window — "
                "meta.json unchanged (winning parameters preserved)"
            )
            return 0

        records = training_record_count(store)

        best = run_permutation_sweep(losses, history, cycles=cycles)
        if best is None or best.losses_blocked < best.loss_total:
            log_engine(
                "synthetic_replay: permutation sweep could not block all losses — "
                "meta.json unchanged"
            )
            return 0

        strict_bounds = best.filters.active_bounds()
        meta = load_meta(meta_path)
        meta.setdefault("features", list(FEATURE_KEYS))
        meta["filter_overrides"] = strict_bounds
        progressive_mode = (
            "strict"
            if records >= ML_MIN_TRAINING_RECORDS
            else "baseline"
            if records < ML_RAMP_MIN_RECORDS
            else "ramp"
        )
        meta["synthetic_replay"] = {
            "updated_at": MLTrainingStore.iso_now(),
            "lookback_hours": lookback_hours,
            "loss_count": len(losses),
            "history_count": len(history),
            "training_record_count": records,
            "progressive_mode": progressive_mode,
            "permutations": cycles,
            "loss_block_rate": round(best.losses_blocked / max(best.loss_total, 1), 4),
            "win_block_rate": round(best.wins_blocked / max(best.win_total, 1), 4),
            "strict_filter_overrides": strict_bounds,
            "source_store": str(store.path),
        }
        write_meta(meta, meta_path)
        log_engine(
            "synthetic_replay: wrote filter_overrides to meta.json "
            f"records={records} progressive_mode={progressive_mode} "
            f"loss_block={best.losses_blocked}/{best.loss_total} "
            f"win_block={best.wins_blocked}/{best.win_total} "
            f"strict_bounds={strict_bounds}"
        )
        return 0
    except Exception as exc:
        log_engine(f"synthetic_replay failed: {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    return run_synthetic_replay()


if __name__ == "__main__":
    raise SystemExit(main())
