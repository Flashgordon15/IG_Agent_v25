"""
Per-epic trailing-stop tuning from replay MFE/MAE (6-bar forward envelope).

Targets Japan 225 and Spot Gold first — sweeps trail_trigger and trail_distance
ATR multiples to maximise median capture_ratio (realised_R / MFE_R).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.walk_forward import load_replay_rows

# Primary v26 trail-tune epics (Phase 2.4)
TARGET_EPICS: dict[str, dict[str, Any]] = {
    "IX.D.NIKKEI.IFM.IP": {"market": "Japan 225", "default_stop_pts": 45},
    "CS.D.CFPGOLD.CFP.IP": {"market": "Spot Gold", "default_stop_pts": 10},
}

_TRIGGER_SWEEP = [0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.2]
_DISTANCE_SWEEP = [0.8, 1.0, 1.25, 1.5, 1.75, 2.0]
_CAPTURE_TARGET = 0.55


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _stop_pts_for_row(row: dict[str, Any], epic: str) -> float:
    raw = row.get("stop_pts")
    if raw is not None:
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    try:
        atr = float(row.get("atr") or 0)
        ratio = float(row.get("atr_ratio") or 0)
        if atr > 0 and ratio > 0:
            return atr / ratio
    except (TypeError, ValueError):
        pass
    meta = TARGET_EPICS.get(epic) or {}
    return float(meta.get("default_stop_pts") or 45)


def mfe_mae_points(
    *,
    direction: str,
    entry: float,
    fwd_high: float,
    fwd_low: float,
) -> tuple[float, float]:
    side = str(direction or "").upper()
    if side == "BUY":
        return max(0.0, fwd_high - entry), max(0.0, entry - fwd_low)
    if side == "SELL":
        return max(0.0, entry - fwd_low), max(0.0, fwd_high - entry)
    return 0.0, 0.0


def simulate_trail_r(
    row: dict[str, Any],
    *,
    trigger_mult: float,
    distance_mult: float,
    stop_pts: float,
) -> float:
    """Two-path envelope simulation (adverse-first + favourable-first average)."""
    direction = str(row.get("direction") or "").upper()
    try:
        entry = float(row.get("entry") or 0)
        atr = float(row.get("atr") or 0)
        fh = float(row.get("fwd_high_6") or 0)
        fl = float(row.get("fwd_low_6") or 0)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0 or atr <= 0 or stop_pts <= 0 or fh <= 0 or fl <= 0:
        return 0.0

    trigger = trigger_mult * atr
    distance = distance_mult * atr
    paths: list[float] = []

    if direction == "BUY":
        init_stop = entry - stop_pts
        for order in ("adverse_first", "favourable_first"):
            if order == "adverse_first":
                if fl <= init_stop:
                    paths.append(-1.0)
                    continue
                if fh - entry < trigger:
                    paths.append((fl - entry) / stop_pts)
                    continue
                trail = max(fh - distance, init_stop)
                paths.append((max(fl, trail) - entry) / stop_pts)
            else:
                if fh - entry >= trigger:
                    trail = max(fh - distance, init_stop)
                    paths.append((max(fl, trail) - entry) / stop_pts)
                elif fl <= init_stop:
                    paths.append(-1.0)
                else:
                    paths.append((fl - entry) / stop_pts)
    elif direction == "SELL":
        init_stop = entry + stop_pts
        for order in ("adverse_first", "favourable_first"):
            if order == "adverse_first":
                if fh >= init_stop:
                    paths.append(-1.0)
                    continue
                if entry - fl < trigger:
                    paths.append((entry - fh) / stop_pts)
                    continue
                trail = min(fl + distance, init_stop)
                paths.append((entry - min(fh, trail)) / stop_pts)
            else:
                if entry - fl >= trigger:
                    trail = min(fl + distance, init_stop)
                    paths.append((entry - min(fh, trail)) / stop_pts)
                elif fh >= init_stop:
                    paths.append(-1.0)
                else:
                    paths.append((entry - fh) / stop_pts)
    else:
        return 0.0

    return sum(paths) / len(paths) if paths else 0.0


def _median_capture(
    rows: list[dict[str, Any]],
    *,
    trigger_mult: float,
    distance_mult: float,
) -> tuple[float, float]:
    captures: list[float] = []
    realised: list[float] = []
    for row in rows:
        epic = str(row.get("epic") or "")
        stop_pts = _stop_pts_for_row(row, epic)
        mfe_pts, _ = mfe_mae_points(
            direction=str(row.get("direction") or ""),
            entry=float(row.get("entry") or 0),
            fwd_high=float(row.get("fwd_high_6") or 0),
            fwd_low=float(row.get("fwd_low_6") or 0),
        )
        mfe_r = mfe_pts / stop_pts if stop_pts > 0 else 0.0
        if mfe_r < 0.05:
            continue
        r = simulate_trail_r(
            row,
            trigger_mult=trigger_mult,
            distance_mult=distance_mult,
            stop_pts=stop_pts,
        )
        realised.append(r)
        captures.append(max(0.0, min(1.5, r / mfe_r)))
    if not captures:
        return 0.0, 0.0
    return statistics.median(captures), statistics.median(realised)


def tune_epic(
    *,
    epic: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    fired = [r for r in rows if r.get("fired")]
    meta = TARGET_EPICS.get(epic) or {}
    stop_default = float(meta.get("default_stop_pts") or 45)

    mfe_rs: list[float] = []
    mae_rs: list[float] = []
    for row in fired:
        stop_pts = _stop_pts_for_row(row, epic)
        mfe_pts, mae_pts = mfe_mae_points(
            direction=str(row.get("direction") or ""),
            entry=float(row.get("entry") or 0),
            fwd_high=float(row.get("fwd_high_6") or 0),
            fwd_low=float(row.get("fwd_low_6") or 0),
        )
        if stop_pts > 0:
            mfe_rs.append(mfe_pts / stop_pts)
            mae_rs.append(mae_pts / stop_pts)

    med_atr = (
        statistics.median(
            [float(r.get("atr") or 0) for r in fired if float(r.get("atr") or 0) > 0]
        )
        if fired
        else 0.0
    )
    med_mfe_pts = (
        statistics.median(
            [
                mfe_mae_points(
                    direction=str(r.get("direction") or ""),
                    entry=float(r.get("entry") or 0),
                    fwd_high=float(r.get("fwd_high_6") or 0),
                    fwd_low=float(r.get("fwd_low_6") or 0),
                )[0]
                for r in fired
            ]
        )
        if fired
        else 0.0
    )
    med_mae_pts = (
        statistics.median(
            [
                mfe_mae_points(
                    direction=str(r.get("direction") or ""),
                    entry=float(r.get("entry") or 0),
                    fwd_high=float(r.get("fwd_high_6") or 0),
                    fwd_low=float(r.get("fwd_low_6") or 0),
                )[1]
                for r in fired
            ]
        )
        if fired
        else 0.0
    )

    curve: list[dict[str, Any]] = []
    best_trigger = 0.75
    best_distance = 1.5
    best_capture = -1.0
    best_median_r = 0.0

    for trig in _TRIGGER_SWEEP:
        for dist in _DISTANCE_SWEEP:
            cap, med_r = _median_capture(fired, trigger_mult=trig, distance_mult=dist)
            curve.append(
                {
                    "trail_trigger_atr_multiple": trig,
                    "trail_distance_atr_multiple": dist,
                    "median_capture_ratio": round(cap, 4),
                    "median_realised_r": round(med_r, 4),
                }
            )
            if cap > best_capture or (
                abs(cap - best_capture) < 1e-6 and med_r > best_median_r
            ):
                best_capture = cap
                best_median_r = med_r
                best_trigger = trig
                best_distance = dist

    if best_capture <= 0 and med_atr > 0:
        # Envelope sweep inconclusive (common on wide-stop indices) — MFE/MAE heuristic.
        best_trigger = max(0.5, min(1.2, (med_mfe_pts * 0.35) / med_atr))
        best_distance = max(0.8, min(2.0, (med_mae_pts * 0.45) / med_atr))
        best_capture = 0.0
        best_median_r = 0.0

    return {
        "epic": epic,
        "market": meta.get("market") or epic,
        "fired_signals": len(fired),
        "default_stop_pts": stop_default,
        "mfe_r_median": round(statistics.median(mfe_rs), 4) if mfe_rs else 0.0,
        "mae_r_median": round(statistics.median(mae_rs), 4) if mae_rs else 0.0,
        "trail_trigger_atr_multiple": best_trigger,
        "trail_distance_atr_multiple": best_distance,
        "median_capture_ratio": round(best_capture, 4),
        "median_realised_r": round(best_median_r, 4),
        "capture_target": _CAPTURE_TARGET,
        "meets_capture_target": best_capture >= _CAPTURE_TARGET,
        "tune_method": "mfe_mae_heuristic" if best_capture <= 0 else "replay_sweep",
        "curve": curve,
    }


def run_trail_tune(
    *,
    epics: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = rows if rows is not None else load_replay_rows()
    targets = epics or list(TARGET_EPICS.keys())
    by_epic: dict[str, list[dict[str, Any]]] = {e: [] for e in targets}
    for row in data:
        epic = str(row.get("epic") or "")
        if epic in by_epic:
            by_epic[epic].append(row)

    results: dict[str, Any] = {}
    for epic in targets:
        epic_rows = by_epic.get(epic) or []
        if not epic_rows:
            continue
        results[epic] = tune_epic(epic=epic, rows=epic_rows)

    defaults = _load_trailing_defaults()
    return {
        "ok": bool(results),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capture_target": _CAPTURE_TARGET,
        "default_trail_trigger_atr_multiple": defaults.get(
            "trail_trigger_atr_multiple", 0.75
        ),
        "default_trail_distance_atr_multiple": defaults.get(
            "trail_distance_atr_multiple", 1.5
        ),
        "by_epic": results,
        "epics_tuned": len(results),
    }


def _load_trailing_defaults() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        block = (raw or {}).get("trailing_stop") or {}
        return block if isinstance(block, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_trail_tune_snapshot(
    *,
    epics: list[str] | None = None,
) -> Path:
    payload = run_trail_tune(epics=epics)
    out_dir = _project_root() / "data_lake" / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trail_epic_overrides.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
