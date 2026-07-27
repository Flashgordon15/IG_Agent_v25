"""Read-only ML / strategy scorecard — is edge measurable and working?

Joins daily journal closes, ml_trade_outcomes, optional loss autopsy APP/LOGIC
mix, and strategy_improvement snapshot into one verdict. Never trains, never
mutates config, never starts trading.

Verdicts
--------
NOT_MEASURABLE — HoldSec / MlScoreAtEntry too sparse (or clean sample too small)
APP_BLOCKED    — autopsy APP share dominates (policy/stamp breaches)
NO_EDGE        — measurable sample but WR / expectancy / lift bad
EDGE_WEAK      — clean sample mildly positive / thin lift
EDGE_OK        — clean sample with positive expectancy and ML lift

Outputs (under data_dir()/reports/):
  ml_strategy_review_YYYY-MM-DD.md
  ml_strategy_review_YYYY-MM-DD.json

CLI::

  PYTHONPATH=src python -m diagnostics.ml_strategy_review --day YYYY-MM-DD
  PYTHONPATH=src python scripts/ml_strategy_review.py --day YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Thresholds (measurement gates — tune via code review, not live config)
# ---------------------------------------------------------------------------
MIN_HOLD_STAMP_PCT = 0.40
MIN_ML_STAMP_PCT = 0.35
MIN_CLEAN_CLOSES = 8
MIN_SCORED_FOR_LIFT = 12
APP_BLOCK_SHARE = 0.40  # APP / (APP+LOGIC+UNKNOWN) among losers
EDGE_OK_WR = 0.55
EDGE_OK_AVG_PNL = 0.05
EDGE_WEAK_WR = 0.45
EDGE_WEAK_AVG_PNL = 0.0
ZERO_PNL_EPS = 1e-9


class ReviewVerdict(str, Enum):
    NOT_MEASURABLE = "NOT_MEASURABLE"
    APP_BLOCKED = "APP_BLOCKED"
    NO_EDGE = "NO_EDGE"
    EDGE_WEAK = "EDGE_WEAK"
    EDGE_OK = "EDGE_OK"


@dataclass
class MeasurementHealth:
    closes: int = 0
    hold_stamped: int = 0
    ml_stamped: int = 0
    hold_stamp_pct: float = 0.0
    ml_stamp_pct: float = 0.0
    zero_pnl: int = 0
    clean_closes: int = 0
    stamp_gate_ok: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class StrategyEdge:
    n: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    wr_pct: float = 0.0
    net_gbp: float = 0.0
    avg_pnl_gbp: float = 0.0
    expectancy_gbp: float = 0.0
    last_20: dict[str, Any] = field(default_factory=dict)
    last_50: dict[str, Any] = field(default_factory=dict)
    by_exit_reason: dict[str, Any] = field(default_factory=dict)
    contaminated_improvement: bool = False
    improvement_note: str = ""


@dataclass
class MlLift:
    scored_n: int = 0
    invalid_score_n: int = 0
    label_count: int | None = None
    last_retrain_age_sec: float | None = None
    buckets: list[dict[str, Any]] = field(default_factory=list)
    lift_high_minus_low_wr: float | None = None
    lift_positive: bool = False
    calibration_status: str = "insufficient_data"
    brier_score: float | None = None
    expected_calibration_error: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class VetoRegret:
    status: str = "insufficient_data"
    available: bool = False
    veto_events: int = 0
    matched_counterfactuals: int = 0
    regretted_vetoes: int = 0
    avoided_losses: int = 0
    flat_counterfactuals: int = 0
    regret_rate: float | None = None
    counterfactual_net_gbp: float | None = None
    by_policy: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class LossMix:
    app: int = 0
    logic: int = 0
    unknown: int = 0
    losers: int = 0
    app_share: float = 0.0
    autopsy_path: str | None = None
    autopsy_verdict: str | None = None
    available: bool = False


def default_data_root() -> Path:
    from system.paths import data_dir

    return Path(data_dir())


def _today_london() -> str:
    return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")


def _f(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _s(val: Any) -> str:
    return str(val or "").strip()


def _parse_day_ts(ts: Any) -> str | None:
    """Return YYYY-MM-DD in UTC if parseable."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return None
    text = str(ts).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        # epoch as string
        return datetime.fromtimestamp(float(text), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def journal_path(data_root: Path) -> Path:
    return data_root / "metrics" / "daily_journal.csv"


def ml_outcomes_path(data_root: Path) -> Path:
    return data_root / "metrics" / "ml_trade_outcomes.jsonl"


def veto_decisions_path(data_root: Path) -> Path:
    return data_root / "metrics" / "ml_veto_decisions.jsonl"


_BLOCKED_IMPROVEMENT_VERDICTS = frozenset(
    {"NOT_MEASURABLE", "APP_BLOCKED", ""}
)


def improvement_epoch_eligible_for_verdict(verdict: str | None) -> bool:
    """True only when a review verdict supports claiming an ML improvement epoch."""
    if verdict is None:
        return False
    v = str(verdict).strip().upper()
    if not v or v in _BLOCKED_IMPROVEMENT_VERDICTS:
        return False
    return True


def load_latest_review_verdict(
    data_root: Path,
    *,
    at_or_before: str | None = None,
) -> tuple[str | None, Path | None]:
    """Return (verdict, path) for the newest ml_strategy_review_*.json under reports/."""
    reports = Path(data_root) / "reports"
    if not reports.is_dir():
        return None, None
    files = sorted(reports.glob("ml_strategy_review_*.json"))
    if not files:
        return None, None
    if at_or_before:
        cutoff = str(at_or_before).strip()
        filtered = [
            p
            for p in files
            if p.stem.replace("ml_strategy_review_", "") <= cutoff
        ]
        files = filtered or files
    path = files[-1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, path
    if not isinstance(raw, dict):
        return None, path
    verdict = _s(raw.get("verdict")).upper() or None
    return verdict, path


def load_veto_decisions(data_root: Path, *, day: str) -> list[dict[str, Any]]:
    """Load structured veto/penalty rows for ``day`` (UTC/London day via ts)."""
    path = veto_decisions_path(data_root)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts_day = _parse_day_ts(row.get("ts"))
                if ts_day is None:
                    ts_day = _parse_day_ts(row.get("timestamp"))
                if ts_day != day:
                    continue
                # Normalize for compute_veto_regret
                if row.get("veto_source") and not row.get("policy"):
                    row = dict(row)
                    row["policy"] = row.get("veto_source")
                if row.get("action") == "veto" and "veto" not in row:
                    row = dict(row)
                    row["veto"] = True
                out.append(row)
    except OSError:
        return []
    return out


def load_gate_funnel_status(data_root: Path) -> dict[str, Any]:
    """Honest funnel availability for the review report (never clobbers disk)."""
    path = Path(data_root) / "gate_funnel_report.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except (OSError, json.JSONDecodeError):
            payload = {}
    try:
        from trading.gate_funnel_counter import classify_funnel_status

        status = classify_funnel_status(payload if payload else None)
    except Exception:
        status = "unavailable" if not payload else "ok"
    return {
        "status": status,
        "path": str(path) if path.is_file() else None,
        "total_ticks": int(payload.get("total_ticks") or 0),
        "all_passed_ticks": int(payload.get("all_passed_ticks") or 0),
        "updated_at": payload.get("updated_at"),
        "pid": payload.get("pid"),
    }


def strategy_improvement_candidates(data_root: Path) -> list[Path]:
    """Prefer data_dir copy; fall back to legacy src/data (contaminated note)."""
    from system.paths import legacy_src_data_dir, project_root

    cands = [
        data_root / "strategy_improvement.json",
        data_root / "metrics" / "strategy_improvement.json",
        legacy_src_data_dir() / "strategy_improvement.json",
        project_root() / "src" / "data" / "strategy_improvement.json",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for p in cands:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def load_journal_closes(data_root: Path, *, day: str) -> list[dict[str, str]]:
    path = journal_path(data_root)
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            deal = _s(row.get("DealID"))
            if not deal or deal.startswith("BENCHMARK"):
                continue
            ts_day = _parse_day_ts(row.get("Timestamp"))
            if ts_day != day:
                continue
            rows.append(dict(row))
    return rows


def load_ml_outcomes(data_root: Path, *, day: str) -> list[dict[str, Any]]:
    path = ml_outcomes_path(data_root)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            ts_day = _parse_day_ts(row.get("ts") or row.get("closed_at") or row.get("timestamp"))
            if ts_day != day:
                continue
            out.append(row)
    return out


def load_loss_autopsy(data_root: Path, *, day: str) -> dict[str, Any] | None:
    path = data_root / "reports" / f"loss_autopsy_{day}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_strategy_improvement(data_root: Path) -> tuple[dict[str, Any] | None, Path | None, bool]:
    """Return (payload, path_used, is_legacy_contaminated_path)."""
    for path in strategy_improvement_candidates(data_root):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        legacy = "v31-production" not in str(path) and path.name == "strategy_improvement.json"
        # Prefer non-legacy when both exist — first candidate wins (data_root first).
        return raw, path, legacy
    return None, None, False


def _ml_index_by_deal(outcomes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for row in outcomes:
        did = _s(row.get("deal_id") or row.get("DealID"))
        if did:
            idx[did] = row
    return idx


def _enrich_close(
    journal_row: dict[str, str],
    ml_row: dict[str, Any] | None,
) -> dict[str, Any]:
    hold = _f(journal_row.get("HoldSec"))
    ml = _f(journal_row.get("MlScoreAtEntry"))
    pnl = _f(journal_row.get("RealizedPnL_GBP"))
    if ml_row:
        if hold is None:
            hold = _f(ml_row.get("hold_sec"))
        if ml is None:
            ml = _f(ml_row.get("ml_score"))
        if pnl is None:
            pnl = _f(ml_row.get("pnl"))
    return {
        "deal_id": _s(journal_row.get("DealID")),
        "timestamp": _s(journal_row.get("Timestamp")),
        "pnl_gbp": pnl,
        "hold_sec": hold,
        "ml_score": ml,
        "exit_reason": _s(journal_row.get("ExitReason")) or _s((ml_row or {}).get("exit_reason")),
        "engine_origin": _s(journal_row.get("EngineOrigin")),
        "style": _s(journal_row.get("Style")),
        "regime": _s(journal_row.get("MarketRegime")),
        "epic": _s((ml_row or {}).get("epic")) or _s(journal_row.get("Epic")),
        "session_slot": _s((ml_row or {}).get("session_slot")),
    }


def compute_measurement_health(closes: list[dict[str, Any]]) -> MeasurementHealth:
    n = len(closes)
    hold_n = sum(1 for c in closes if c.get("hold_sec") is not None)
    ml_n = sum(1 for c in closes if c.get("ml_score") is not None)
    zero_n = sum(
        1
        for c in closes
        if c.get("pnl_gbp") is not None and abs(float(c["pnl_gbp"])) < ZERO_PNL_EPS
    )
    clean = [
        c
        for c in closes
        if c.get("hold_sec") is not None
        and c.get("ml_score") is not None
        and c.get("pnl_gbp") is not None
        and abs(float(c["pnl_gbp"])) >= ZERO_PNL_EPS
    ]
    hold_pct = round(hold_n / n, 4) if n else 0.0
    ml_pct = round(ml_n / n, 4) if n else 0.0
    notes: list[str] = []
    if n == 0:
        notes.append("no journal closes for day")
    if hold_pct < MIN_HOLD_STAMP_PCT:
        notes.append(
            f"HoldSec stamped {hold_n}/{n} ({hold_pct:.0%}) < gate {MIN_HOLD_STAMP_PCT:.0%}"
        )
    if ml_pct < MIN_ML_STAMP_PCT:
        notes.append(
            f"MlScoreAtEntry stamped {ml_n}/{n} ({ml_pct:.0%}) < gate {MIN_ML_STAMP_PCT:.0%}"
        )
    if len(clean) < MIN_CLEAN_CLOSES:
        notes.append(f"clean stamped closes {len(clean)} < min {MIN_CLEAN_CLOSES}")
    stamp_ok = (
        n > 0
        and hold_pct >= MIN_HOLD_STAMP_PCT
        and ml_pct >= MIN_ML_STAMP_PCT
        and len(clean) >= MIN_CLEAN_CLOSES
    )
    return MeasurementHealth(
        closes=n,
        hold_stamped=hold_n,
        ml_stamped=ml_n,
        hold_stamp_pct=hold_pct,
        ml_stamp_pct=ml_pct,
        zero_pnl=zero_n,
        clean_closes=len(clean),
        stamp_gate_ok=stamp_ok,
        notes=notes,
    )


def _window_stats(closes: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [c for c in closes if c.get("pnl_gbp") is not None]
    if not usable:
        return {"n": 0, "wins": 0, "wr_pct": 0.0, "net_gbp": 0.0, "avg_pnl_gbp": 0.0}
    n = len(usable)
    wins = sum(1 for c in usable if float(c["pnl_gbp"]) > ZERO_PNL_EPS)
    net = sum(float(c["pnl_gbp"]) for c in usable)
    return {
        "n": n,
        "wins": wins,
        "wr_pct": round(100.0 * wins / n, 2) if n else 0.0,
        "net_gbp": round(net, 2),
        "avg_pnl_gbp": round(net / n, 4) if n else 0.0,
    }


def compute_strategy_edge(
    closes: list[dict[str, Any]],
    *,
    improvement: dict[str, Any] | None,
    improvement_path: Path | None,
    improvement_legacy: bool,
) -> StrategyEdge:
    usable = [c for c in closes if c.get("pnl_gbp") is not None]
    wins = sum(1 for c in usable if float(c["pnl_gbp"]) > ZERO_PNL_EPS)
    losses = sum(1 for c in usable if float(c["pnl_gbp"]) < -ZERO_PNL_EPS)
    flat = sum(1 for c in usable if abs(float(c["pnl_gbp"])) < ZERO_PNL_EPS)
    net = sum(float(c["pnl_gbp"]) for c in usable)
    n = len(usable)
    avg = (net / n) if n else 0.0
    by_exit: Counter[str] = Counter()
    for c in usable:
        reason = _s(c.get("exit_reason")) or "unstamped"
        by_exit[reason.split()[0] if reason else "unstamped"] += 1

    contaminated = False
    note = ""
    if improvement is not None:
        closes_si = improvement.get("closes") or []
        if isinstance(closes_si, list) and closes_si:
            null_hold = sum(1 for r in closes_si if r.get("hold_sec") is None)
            wins_si = sum(1 for r in closes_si if r.get("won"))
            if null_hold == len(closes_si) or (wins_si == 0 and null_hold > len(closes_si) * 0.8):
                contaminated = True
                note = (
                    f"strategy_improvement snapshot contaminated "
                    f"(closes={len(closes_si)}, hold_sec null={null_hold}, wins={wins_si})"
                )
        if improvement_legacy and improvement_path is not None:
            note = (note + "; " if note else "") + (
                f"read from legacy path {improvement_path} — prefer data_dir()"
            )
    elif improvement_path is None:
        note = "strategy_improvement.json not found under data_dir() or legacy"

    return StrategyEdge(
        n=n,
        wins=wins,
        losses=losses,
        flat=flat,
        wr_pct=round(100.0 * wins / n, 2) if n else 0.0,
        net_gbp=round(net, 2),
        avg_pnl_gbp=round(avg, 4),
        expectancy_gbp=round(avg, 4),
        last_20=_window_stats(usable[-20:]),
        last_50=_window_stats(usable[-50:]),
        by_exit_reason=dict(by_exit.most_common(12)),
        contaminated_improvement=contaminated,
        improvement_note=note,
    )


def compute_ml_lift(closes: list[dict[str, Any]], *, improvement: dict[str, Any] | None) -> MlLift:
    raw_scored = [
        c for c in closes if c.get("ml_score") is not None and c.get("pnl_gbp") is not None
    ]
    scored = [
        c
        for c in raw_scored
        if 0.0 <= float(c["ml_score"]) <= 1.0
    ]
    invalid_n = len(raw_scored) - len(scored)
    notes: list[str] = []
    label_count: int | None = None
    retrain_age: float | None = None
    if improvement:
        try:
            label_count = int(improvement.get("ml_label_count") or 0) or None
        except (TypeError, ValueError):
            label_count = None
        # live snapshot may expose last_model_train_ts on disk payload
        train_ts = improvement.get("last_model_train_ts")
        if train_ts:
            try:
                retrain_age = max(0.0, datetime.now(tz=timezone.utc).timestamp() - float(train_ts))
            except (TypeError, ValueError):
                retrain_age = None
    if invalid_n:
        notes.append(
            f"excluded {invalid_n} MlScoreAtEntry value(s) outside [0,1] from lift/calibration"
        )
    if not scored:
        notes.append("no scored closes for lift")
        return MlLift(
            scored_n=0,
            invalid_score_n=invalid_n,
            label_count=label_count,
            last_retrain_age_sec=retrain_age,
            notes=notes,
        )

    ordered = sorted(scored, key=lambda c: float(c["ml_score"]))
    scores = [float(c["ml_score"]) for c in ordered]
    # Equal-frequency quintiles when enough; else a median split.
    n = len(scored)
    if n >= 25:
        labels = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    elif n >= MIN_SCORED_FOR_LIFT:
        labels = ["LOW", "HIGH"]
    else:
        notes.append(f"scored_n={n} < {MIN_SCORED_FOR_LIFT} — lift not reliable")
        wins = sum(1 for c in scored if float(c["pnl_gbp"]) > ZERO_PNL_EPS)
        mean_score = sum(scores) / n
        observed = wins / n
        buckets = [
            {
                "bucket": "ALL",
                "n": n,
                "wr_pct": round(100.0 * observed, 2),
                "observed_win_rate": round(observed, 4),
                "mean_score": round(mean_score, 4),
                "calibration_gap_pp": round(100.0 * (observed - mean_score), 2),
                "avg_pnl_gbp": round(sum(float(c["pnl_gbp"]) for c in scored) / n, 4),
                "ml_lo": round(scores[0], 4),
                "ml_hi": round(scores[-1], 4),
            }
        ]
        return MlLift(
            scored_n=n,
            invalid_score_n=invalid_n,
            label_count=label_count,
            last_retrain_age_sec=retrain_age,
            buckets=buckets,
            lift_high_minus_low_wr=None,
            lift_positive=False,
            calibration_status="insufficient_data",
            notes=notes,
        )

    grouped: dict[str, list[dict[str, Any]]] = {lab: [] for lab in labels}
    for i, c in enumerate(ordered):
        bucket_i = min(len(labels) - 1, (i * len(labels)) // n)
        grouped[labels[bucket_i]].append(c)

    buckets: list[dict[str, Any]] = []
    brier_sum = 0.0
    ece = 0.0
    for lab in labels:
        rows = grouped[lab]
        if not rows:
            continue
        bn = len(rows)
        wins = sum(1 for r in rows if float(r["pnl_gbp"]) > ZERO_PNL_EPS)
        avg = sum(float(r["pnl_gbp"]) for r in rows) / bn
        svals = [float(r["ml_score"]) for r in rows]
        observed = wins / bn
        mean_score = sum(svals) / bn
        brier = sum(
            (float(r["ml_score"]) - (1.0 if float(r["pnl_gbp"]) > ZERO_PNL_EPS else 0.0))
            ** 2
            for r in rows
        ) / bn
        brier_sum += brier * bn
        ece += (bn / n) * abs(observed - mean_score)
        buckets.append(
            {
                "bucket": lab,
                "n": bn,
                "wr_pct": round(100.0 * wins / bn, 2),
                "observed_win_rate": round(observed, 4),
                "mean_score": round(mean_score, 4),
                "calibration_gap_pp": round(100.0 * (observed - mean_score), 2),
                "brier_score": round(brier, 4),
                "avg_pnl_gbp": round(avg, 4),
                "ml_lo": round(min(svals), 4),
                "ml_hi": round(max(svals), 4),
            }
        )

    lift: float | None = None
    lift_pos = False
    if len(buckets) >= 2:
        low = buckets[0]
        high = buckets[-1]
        score_separation = float(high["mean_score"]) - float(low["mean_score"])
        if score_separation <= 1e-9:
            notes.append("bucket scores have no separation — lift delta unavailable")
        elif low["n"] >= 3 and high["n"] >= 3:
            lift = round(float(high["wr_pct"]) - float(low["wr_pct"]), 2)
            lift_pos = lift > 5.0 and float(high["avg_pnl_gbp"]) > float(low["avg_pnl_gbp"])
        else:
            notes.append("bucket sizes too small for lift delta")
    return MlLift(
        scored_n=n,
        invalid_score_n=invalid_n,
        label_count=label_count,
        last_retrain_age_sec=round(retrain_age, 1) if retrain_age is not None else None,
        buckets=buckets,
        lift_high_minus_low_wr=lift,
        lift_positive=lift_pos,
        calibration_status="ok",
        brier_score=round(brier_sum / n, 4),
        expected_calibration_error=round(ece, 4),
        notes=notes,
    )


def _veto_policy(row: dict[str, Any]) -> str | None:
    action = _s(row.get("action")).lower()
    if action == "penalty":
        return None
    for key in ("veto_source", "veto_policy", "policy", "mode"):
        value = _s(row.get(key))
        if not value:
            continue
        if action == "veto" or "veto" in value.lower() or bool(row.get("veto")):
            return value
    for key in ("setup_memory", "profit_policy"):
        value = row.get(key)
        if isinstance(value, dict) and bool(value.get("veto")):
            return key
    if bool(row.get("veto")) or action == "veto":
        return _s(row.get("veto_source")) or "unknown_veto"
    return None


def compute_clean_expectancy(
    closes: list[dict[str, Any]],
    *,
    dimensions: tuple[str, ...] = ("exit_reason", "epic", "style", "regime"),
) -> dict[str, Any]:
    """Read-only expectancy buckets on clean stamped closes (no parameter changes)."""
    clean = [
        c
        for c in closes
        if c.get("hold_sec") is not None
        and c.get("ml_score") is not None
        and 0.0 <= float(c["ml_score"]) <= 1.0
        and c.get("pnl_gbp") is not None
        and abs(float(c["pnl_gbp"])) >= ZERO_PNL_EPS
    ]

    def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        if not n:
            return {"n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "net_gbp": 0.0, "avg_pnl_gbp": 0.0}
        wins = sum(1 for r in rows if float(r["pnl_gbp"]) > ZERO_PNL_EPS)
        losses = sum(1 for r in rows if float(r["pnl_gbp"]) < -ZERO_PNL_EPS)
        net = sum(float(r["pnl_gbp"]) for r in rows)
        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "wr_pct": round(100.0 * wins / n, 2),
            "net_gbp": round(net, 4),
            "avg_pnl_gbp": round(net / n, 4),
        }

    by_dim: dict[str, dict[str, Any]] = {}
    for dim in dimensions:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in clean:
            key = _s(row.get(dim)) or "(unknown)"
            groups.setdefault(key, []).append(row)
        by_dim[dim] = {
            key: _bucket(rows)
            for key, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        }

    return {
        "clean_n": len(clean),
        "overall": _bucket(clean),
        "by": by_dim,
        "notes": [
            "clean sample requires HoldSec + MlScore in [0,1] + non-zero GBP PnL",
            "read-only analytics — no strategy parameter changes",
        ],
    }


def compute_veto_regret(outcomes: list[dict[str, Any]]) -> VetoRegret:
    """Summarize labelled veto counterfactuals; never infer skipped outcomes."""
    vetoes: list[tuple[str, float | None]] = []
    for row in outcomes:
        policy = _veto_policy(row)
        if policy is None:
            continue
        counterfactual = None
        for key in ("counterfactual_pnl", "shadow_pnl", "pnl_if_taken"):
            counterfactual = _f(row.get(key))
            if counterfactual is not None:
                break
        vetoes.append((policy, counterfactual))

    matched = [(policy, pnl) for policy, pnl in vetoes if pnl is not None]
    if not matched:
        return VetoRegret(
            veto_events=len(vetoes),
            notes=[
                "insufficient_data: no structured veto event with labelled "
                "counterfactual_pnl/shadow_pnl/pnl_if_taken; taken-trade PnL is not veto regret"
            ],
        )

    by_policy_rows: dict[str, list[float]] = {}
    for policy, pnl in matched:
        by_policy_rows.setdefault(policy, []).append(float(pnl))
    by_policy: dict[str, Any] = {}
    for policy, values in sorted(by_policy_rows.items()):
        by_policy[policy] = {
            "n": len(values),
            "regretted_vetoes": sum(1 for value in values if value > ZERO_PNL_EPS),
            "avoided_losses": sum(1 for value in values if value < -ZERO_PNL_EPS),
            "counterfactual_net_gbp": round(sum(values), 4),
        }

    values = [float(pnl) for _, pnl in matched]
    regrets = sum(1 for pnl in values if pnl > ZERO_PNL_EPS)
    avoided = sum(1 for pnl in values if pnl < -ZERO_PNL_EPS)
    flat = len(values) - regrets - avoided
    return VetoRegret(
        status="ok",
        available=True,
        veto_events=len(vetoes),
        matched_counterfactuals=len(values),
        regretted_vetoes=regrets,
        avoided_losses=avoided,
        flat_counterfactuals=flat,
        regret_rate=round(regrets / len(values), 4),
        counterfactual_net_gbp=round(sum(values), 4),
        by_policy=by_policy,
    )


def compute_loss_mix(autopsy: dict[str, Any] | None, *, autopsy_path: Path | None) -> LossMix:
    if not autopsy:
        return LossMix(available=False)
    fund = autopsy.get("fundamentals_followed") or {}
    summary = autopsy.get("summary") or {}
    by_class = summary.get("by_loss_class") or {}
    app = int(fund.get("app") or by_class.get("APP") or 0)
    logic = int(fund.get("logic") or by_class.get("LOGIC") or 0)
    unknown = int(fund.get("unknown") or by_class.get("UNKNOWN") or 0)
    losers = int(summary.get("losers") or (app + logic + unknown) or 0)
    denom = app + logic + unknown
    share = round(app / denom, 4) if denom else 0.0
    return LossMix(
        app=app,
        logic=logic,
        unknown=unknown,
        losers=losers,
        app_share=share,
        autopsy_path=str(autopsy_path) if autopsy_path else None,
        autopsy_verdict=_s(fund.get("verdict")) or None,
        available=True,
    )


def decide_verdict(
    health: MeasurementHealth,
    edge: StrategyEdge,
    lift: MlLift,
    loss_mix: LossMix,
) -> tuple[ReviewVerdict, str]:
    """Return (verdict, next_one_step hint). Never auto-applies changes."""
    if not health.stamp_gate_ok:
        return (
            ReviewVerdict.NOT_MEASURABLE,
            "APP: fix HoldSec + MlScoreAtEntry journal stamps on close path; "
            "re-run review when stamp gates clear — do not loosen strategy yet",
        )

    if loss_mix.available and loss_mix.app_share >= APP_BLOCK_SHARE and loss_mix.app > 0:
        return (
            ReviewVerdict.APP_BLOCKED,
            "APP: triage top autopsy APP tickets (policy/path/stamp breaches) "
            "before any LOGIC or ML lead change",
        )

    # Prefer clean subset metrics when available
    wr = edge.wr_pct / 100.0
    avg = edge.expectancy_gbp
    if (
        health.clean_closes >= MIN_CLEAN_CLOSES
        and wr >= EDGE_OK_WR
        and avg >= EDGE_OK_AVG_PNL
        and (lift.lift_positive or lift.scored_n < MIN_SCORED_FOR_LIFT)
    ):
        return (
            ReviewVerdict.EDGE_OK,
            "EDGE_OK: keep measurement cadence; ML lead only after APP rate stays low",
        )

    if health.clean_closes >= MIN_CLEAN_CLOSES and (
        (wr >= EDGE_WEAK_WR and avg >= EDGE_WEAK_AVG_PNL) or lift.lift_positive
    ):
        return (
            ReviewVerdict.EDGE_WEAK,
            "EDGE_WEAK: one LOGIC hypothesis only after APP quiet — do not mega-tune",
        )

    return (
        ReviewVerdict.NO_EDGE,
        "NO_EDGE: measurable but expectancy/WR poor — one LOGIC knob after APP clear; "
        "never auto-resume under bleed lock",
    )


def build_ml_strategy_review(
    *,
    day: str,
    data_root: Path | None = None,
) -> dict[str, Any]:
    root = data_root or default_data_root()
    journal_rows = load_journal_closes(root, day=day)
    ml_rows = load_ml_outcomes(root, day=day)
    ml_idx = _ml_index_by_deal(ml_rows)
    closes = [
        _enrich_close(j, ml_idx.get(_s(j.get("DealID"))))
        for j in journal_rows
    ]
    # Include ML-only day rows missing from journal (best-effort)
    journal_ids = {_s(c.get("deal_id")) for c in closes}
    for m in ml_rows:
        did = _s(m.get("deal_id"))
        if not did or did in journal_ids:
            continue
        closes.append(
            {
                "deal_id": did,
                "timestamp": "",
                "pnl_gbp": _f(m.get("pnl")),
                "hold_sec": _f(m.get("hold_sec")),
                "ml_score": _f(m.get("ml_score")),
                "exit_reason": _s(m.get("exit_reason")),
                "engine_origin": _s(m.get("engine_origin")),
                "style": _s(m.get("style")),
                "regime": _s(m.get("regime")),
                "epic": _s(m.get("epic")),
                "session_slot": _s(m.get("session_slot")),
            }
        )

    autopsy_path = root / "reports" / f"loss_autopsy_{day}.json"
    autopsy = load_loss_autopsy(root, day=day)
    improvement, improvement_path, improvement_legacy = load_strategy_improvement(root)

    # Optionally merge live tracker snapshot fields (read-only)
    snap: dict[str, Any] | None = None
    try:
        from runtime.strategy_improvement_tracker import snapshot as si_snapshot

        snap = si_snapshot(window=20)
        if improvement is None and isinstance(snap, dict):
            improvement = {
                "closes": snap.get("recent_closes") or [],
                "ml_label_count": snap.get("ml_label_count"),
                "strategy_epoch": snap.get("strategy_epoch"),
            }
            improvement_legacy = False
        elif isinstance(snap, dict) and improvement is not None:
            improvement = dict(improvement)
            improvement.setdefault("ml_label_count", snap.get("ml_label_count"))
    except Exception:
        snap = None

    health = compute_measurement_health(closes)
    edge = compute_strategy_edge(
        closes,
        improvement=improvement,
        improvement_path=improvement_path,
        improvement_legacy=improvement_legacy,
    )
    lift = compute_ml_lift(closes, improvement=improvement)
    if not health.stamp_gate_ok and lift.scored_n:
        lift.calibration_status = "descriptive_only_stamp_gate_failed"
        lift.notes.append(
            "calibration/lift is descriptive only until HoldSec/MlScore clean-close gates pass"
        )
    veto_rows = load_veto_decisions(root, day=day)
    # Prefer durable veto decision log; fall back to ml_trade_outcomes tags.
    veto_regret = compute_veto_regret(veto_rows if veto_rows else ml_rows)
    if veto_rows and veto_regret.status == "insufficient_data":
        veto_regret.notes = list(veto_regret.notes) + [
            f"veto_log_events={len(veto_rows)} but no labelled counterfactual_pnl/shadow_pnl/pnl_if_taken"
        ]
    clean_expectancy = compute_clean_expectancy(closes)
    gate_funnel = load_gate_funnel_status(root)
    loss_mix = compute_loss_mix(
        autopsy, autopsy_path=autopsy_path if autopsy_path.is_file() else None
    )
    verdict, next_step = decide_verdict(health, edge, lift, loss_mix)

    return {
        "ok": True,
        "day": day,
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "data_root": str(root),
        "verdict": verdict.value,
        "next_one_step": next_step,
        "measurement_health": asdict(health),
        "strategy_edge": asdict(edge),
        "ml_lift": asdict(lift),
        "veto_regret": asdict(veto_regret),
        "clean_expectancy": clean_expectancy,
        "gate_funnel": gate_funnel,
        "loss_mix": asdict(loss_mix),
        "inputs": {
            "journal_path": str(journal_path(root)),
            "journal_day_rows": len(journal_rows),
            "ml_outcomes_path": str(ml_outcomes_path(root)),
            "ml_outcomes_day_rows": len(ml_rows),
            "veto_decisions_path": str(veto_decisions_path(root)),
            "veto_decisions_day_rows": len(veto_rows),
            "strategy_improvement_path": str(improvement_path) if improvement_path else None,
            "strategy_improvement_legacy": improvement_legacy,
            "loss_autopsy_path": str(autopsy_path) if autopsy_path.is_file() else None,
            "strategy_improvement_snapshot_ok": bool(snap and snap.get("ok")),
            "gate_funnel_status": gate_funnel.get("status"),
        },
        "gates": {
            "min_hold_stamp_pct": MIN_HOLD_STAMP_PCT,
            "min_ml_stamp_pct": MIN_ML_STAMP_PCT,
            "min_clean_closes": MIN_CLEAN_CLOSES,
            "app_block_share": APP_BLOCK_SHARE,
        },
        "read_only": True,
        "auto_apply": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    day = report.get("day")
    verdict = report.get("verdict")
    health = report.get("measurement_health") or {}
    edge = report.get("strategy_edge") or {}
    lift = report.get("ml_lift") or {}
    veto = report.get("veto_regret") or {}
    loss = report.get("loss_mix") or {}
    inputs = report.get("inputs") or {}
    lines = [
        f"# ML / Strategy Review — {day}",
        "",
        f"**Verdict:** `{verdict}`  ",
        f"**Generated:** {report.get('generated_at')}  ",
        f"**Data root:** `{report.get('data_root')}`  ",
        "",
        "> Read-only scorecard. Does not train, mutate config, or start trading.",
        "",
        "## Next one step",
        "",
        str(report.get("next_one_step") or ""),
        "",
        "## Measurement health",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Closes | {health.get('closes')} |",
        f"| HoldSec stamped | {health.get('hold_stamped')} ({float(health.get('hold_stamp_pct') or 0):.0%}) |",
        f"| MlScore stamped | {health.get('ml_stamped')} ({float(health.get('ml_stamp_pct') or 0):.0%}) |",
        f"| Clean closes | {health.get('clean_closes')} |",
        f"| £0 PnL rows | {health.get('zero_pnl')} |",
        f"| Stamp gate OK | {health.get('stamp_gate_ok')} |",
        "",
    ]
    notes = health.get("notes") or []
    if notes:
        lines.append("Notes:")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.extend(
        [
            "## Strategy edge",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| N / W / L / flat | {edge.get('n')} / {edge.get('wins')} / {edge.get('losses')} / {edge.get('flat')} |",
            f"| WR | {edge.get('wr_pct')}% |",
            f"| Net £ | {edge.get('net_gbp')} |",
            f"| Avg / expectancy £ | {edge.get('avg_pnl_gbp')} |",
            f"| Last 20 | {edge.get('last_20')} |",
            f"| Last 50 | {edge.get('last_50')} |",
            f"| Contaminated improvement file | {edge.get('contaminated_improvement')} |",
            "",
        ]
    )
    if edge.get("improvement_note"):
        lines.append(f"Improvement note: {edge.get('improvement_note')}")
        lines.append("")
    by_exit = edge.get("by_exit_reason") or {}
    if by_exit:
        lines.append("Exit reasons (top):")
        for reason, count in list(by_exit.items())[:8]:
            lines.append(f"- `{reason}`: {count}")
        lines.append("")

    lines.extend(
        [
            "## ML lift",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Scored N | {lift.get('scored_n')} |",
            f"| Invalid score N | {lift.get('invalid_score_n')} |",
            f"| Train labels | {lift.get('label_count')} |",
            f"| Retrain age (s) | {lift.get('last_retrain_age_sec')} |",
            f"| High−low WR (pp) | {lift.get('lift_high_minus_low_wr')} |",
            f"| Lift positive | {lift.get('lift_positive')} |",
            f"| Calibration status | {lift.get('calibration_status')} |",
            f"| Brier score | {lift.get('brier_score')} |",
            f"| Expected calibration error | {lift.get('expected_calibration_error')} |",
            "",
        ]
    )
    buckets = lift.get("buckets") or []
    if buckets:
        lines.append("| Bucket | N | Mean score | Observed WR | Gap pp | Avg £ | ML lo | ML hi |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for b in buckets:
            lines.append(
                f"| {b.get('bucket')} | {b.get('n')} | {b.get('mean_score')} | "
                f"{b.get('observed_win_rate')} | {b.get('calibration_gap_pp')} | "
                f"{b.get('avg_pnl_gbp')} | {b.get('ml_lo')} | {b.get('ml_hi')} |"
            )
        lines.append("")
    for n in lift.get("notes") or []:
        lines.append(f"- {n}")
    if lift.get("notes"):
        lines.append("")

    lines.extend(
        [
            "## Veto regret",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Status | {veto.get('status')} |",
            f"| Veto events | {veto.get('veto_events')} |",
            f"| Matched counterfactuals | {veto.get('matched_counterfactuals')} |",
            f"| Regretted vetoes | {veto.get('regretted_vetoes')} |",
            f"| Avoided losses | {veto.get('avoided_losses')} |",
            f"| Regret rate | {veto.get('regret_rate')} |",
            f"| Counterfactual net £ | {veto.get('counterfactual_net_gbp')} |",
            "",
        ]
    )
    for n in veto.get("notes") or []:
        lines.append(f"- {n}")
    if veto.get("notes"):
        lines.append("")

    lines.extend(
        [
            "## Loss mix (APP / LOGIC / UNKNOWN)",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Available | {loss.get('available')} |",
            f"| APP | {loss.get('app')} |",
            f"| LOGIC | {loss.get('logic')} |",
            f"| UNKNOWN | {loss.get('unknown')} |",
            f"| APP share | {float(loss.get('app_share') or 0):.0%} |",
            f"| Autopsy verdict | {loss.get('autopsy_verdict')} |",
            f"| Autopsy path | `{loss.get('autopsy_path')}` |",
            "",
            "## Inputs",
            "",
            f"- Journal: `{inputs.get('journal_path')}` ({inputs.get('journal_day_rows')} rows)",
            f"- ML outcomes: `{inputs.get('ml_outcomes_path')}` ({inputs.get('ml_outcomes_day_rows')} rows)",
            f"- Strategy improvement: `{inputs.get('strategy_improvement_path')}` "
            f"(legacy={inputs.get('strategy_improvement_legacy')})",
            f"- Loss autopsy: `{inputs.get('loss_autopsy_path')}`",
            "",
            "## Verdict enum",
            "",
            "`NOT_MEASURABLE` | `APP_BLOCKED` | `NO_EDGE` | `EDGE_WEAK` | `EDGE_OK`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write_ml_strategy_review(
    *,
    day: str,
    data_root: Path | None = None,
    write_json: bool = True,
) -> tuple[Path, Path | None, dict[str, Any]]:
    root = data_root or default_data_root()
    report = build_ml_strategy_review(day=day, data_root=root)
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"ml_strategy_review_{day}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path: Path | None = None
    if write_json:
        json_path = reports_dir / f"ml_strategy_review_{day}.json"
        json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return md_path, json_path, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only ML/strategy review scorecard (no trading side effects)"
    )
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: London today)")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument(
        "--write",
        action="store_true",
        default=True,
        help="Write reports/ml_strategy_review_YYYY-MM-DD.md(+json) (default: on)",
    )
    ap.add_argument(
        "--no-write",
        action="store_true",
        help="Build report in memory only (print summary)",
    )
    ap.add_argument("--json-stdout", action="store_true", help="Print full JSON to stdout")
    args = ap.parse_args(argv)

    day = args.day or _today_london()
    root = args.data_root or default_data_root()

    if args.no_write:
        report = build_ml_strategy_review(day=day, data_root=root)
        md_path = json_path = None
    else:
        md_path, json_path, report = write_ml_strategy_review(
            day=day, data_root=root, write_json=True
        )

    if args.json_stdout:
        print(json.dumps(report, indent=2, default=str))
    else:
        if md_path:
            print(f"Wrote {md_path}")
        if json_path:
            print(f"Wrote {json_path}")
        print(f"Verdict: {report.get('verdict')}")
        health = report.get("measurement_health") or {}
        edge = report.get("strategy_edge") or {}
        loss = report.get("loss_mix") or {}
        print(
            f"closes={health.get('closes')} clean={health.get('clean_closes')} "
            f"hold%={float(health.get('hold_stamp_pct') or 0):.0%} "
            f"ml%={float(health.get('ml_stamp_pct') or 0):.0%} "
            f"WR={edge.get('wr_pct')}% net={edge.get('net_gbp')} "
            f"APP={loss.get('app')} LOGIC={loss.get('logic')} UNKNOWN={loss.get('unknown')}"
        )
        print(f"Next: {report.get('next_one_step')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
