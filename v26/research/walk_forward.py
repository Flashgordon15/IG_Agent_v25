"""Walk-forward threshold analysis from OHLC signal replay results."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _replay_results_path() -> Path:
    return _project_root() / "src" / "data" / "replay_results.jsonl"


def _label(row: dict[str, Any]) -> str:
    from research.label_utils import outcome_label

    return outcome_label(row)


def _confidence(row: dict[str, Any]) -> float:
    for key in ("adjusted_confidence", "adjusted_score", "confidence", "raw_score"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def load_replay_rows() -> list[dict[str, Any]]:
    path = _replay_results_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def threshold_sweep(
    rows: list[dict[str, Any]],
    *,
    thresholds: list[int] | None = None,
) -> dict[str, Any]:
    """Per-epic WR at each confidence threshold (3-bar label proxy)."""
    thresh = thresholds or [70, 75, 80, 85, 90]
    by_epic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        epic = str(row.get("epic") or "unknown")
        by_epic[epic].append(row)

    epic_results: dict[str, Any] = {}
    for epic, epic_rows in by_epic.items():
        curve: list[dict[str, Any]] = []
        best_thr = 75
        best_wr = -1.0
        for t in thresh:
            subset = [r for r in epic_rows if _confidence(r) >= t]
            wins = sum(1 for r in subset if _label(r) == "WIN")
            losses = sum(1 for r in subset if _label(r) == "LOSS")
            decided = wins + losses
            wr = wins / decided if decided else 0.0
            curve.append(
                {
                    "threshold": t,
                    "n": len(subset),
                    "decided": decided,
                    "wr": round(wr, 4),
                }
            )
            if decided >= 10 and wr > best_wr:
                best_wr = wr
                best_thr = t
        epic_results[epic] = {
            "curve": curve,
            "recommended_threshold": best_thr if best_wr >= 0 else 75,
            "best_wr": round(best_wr, 4) if best_wr >= 0 else None,
            "total_rows": len(epic_rows),
        }

    return {
        "ok": bool(rows),
        "total_rows": len(rows),
        "by_epic": epic_results,
    }


def ml_veto_hints(sweep: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    for epic, data in (sweep.get("by_epic") or {}).items():
        rec = data.get("recommended_threshold")
        wr = data.get("best_wr")
        if wr is not None and rec:
            hints.append(
                f"{epic}: replay WR {wr:.0%} best at ≥{rec}% "
                f"(n={data.get('total_rows')})"
            )
    return hints[:8]
