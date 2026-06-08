"""L1 replay — counterfactual signal frequency from feature store (no IG orders)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def features_signals_path(day: str) -> Path:
    return _project_root() / "data_lake" / "features" / day / "signals.csv"


def replay_day_signals(
    day: str,
    *,
    thresholds: list[float] | None = None,
) -> dict[str, Any]:
    """Count how many evals would fire at each confidence threshold."""
    path = features_signals_path(day)
    if not path.is_file():
        return {
            "day": day,
            "ok": False,
            "error": "signals.csv missing — run build_feature_store",
        }

    df = pd.read_csv(path)
    if df.empty or "adjusted_score" not in df.columns:
        return {"day": day, "ok": False, "error": "no signal rows"}

    scores = pd.to_numeric(df["adjusted_score"], errors="coerce").fillna(0)
    thresh_list = thresholds or [70.0, 75.0, 80.0, 85.0, 90.0, 92.0]
    by_threshold: dict[str, int] = {}
    for t in thresh_list:
        by_threshold[f">={int(t)}"] = int((scores >= t).sum())

    would_fire_col = df.get("would_fire")
    actual_fire = (
        int(would_fire_col.astype(str).str.lower().eq("true").sum())
        if would_fire_col is not None
        else 0
    )

    by_epic: dict[str, int] = {}
    if "epic" in df.columns:
        for epic, grp in df.groupby("epic"):
            s = pd.to_numeric(grp["adjusted_score"], errors="coerce").fillna(0)
            by_epic[str(epic)] = int((s >= 75).sum())

    return {
        "day": day,
        "ok": True,
        "evals": len(df),
        "actual_would_fire": actual_fire,
        "by_threshold": by_threshold,
        "by_epic_at_75": by_epic,
        "median_confidence": round(float(scores.median()), 1),
        "mean_confidence": round(float(scores.mean()), 1),
    }


def replay_days(
    days: list[str],
    *,
    thresholds: list[float] | None = None,
) -> dict[str, Any]:
    rows = [replay_day_signals(d, thresholds=thresholds) for d in days]
    ok_rows = [r for r in rows if r.get("ok")]
    total_evals = sum(int(r.get("evals") or 0) for r in ok_rows)
    total_fire_75 = sum(
        int((r.get("by_threshold") or {}).get(">=75") or 0) for r in ok_rows
    )
    return {
        "days": days,
        "days_ok": len(ok_rows),
        "total_evals": total_evals,
        "total_would_fire_at_75": total_fire_75,
        "daily": rows,
    }
