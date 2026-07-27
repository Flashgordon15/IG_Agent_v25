"""Tests for diagnostics.ml_strategy_review (read-only scorecard)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from diagnostics.ml_strategy_review import (
    ReviewVerdict,
    build_ml_strategy_review,
    compute_measurement_health,
    compute_ml_lift,
    compute_veto_regret,
    decide_verdict,
    MeasurementHealth,
    StrategyEdge,
    MlLift,
    LossMix,
    write_ml_strategy_review,
)


_JOURNAL_HEADER = [
    "Timestamp",
    "DealID",
    "Direction",
    "EntryPrice",
    "ExitPrice",
    "RealizedPnL_GBP",
    "ClosingFillRate",
    "ActiveSlipMultiplier",
    "AccountID",
    "ProductType",
    "EngineOrigin",
    "ExitReason",
    "HoldSec",
    "Style",
    "MlScoreAtEntry",
    "MarketRegime",
]


def _write_journal(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_JOURNAL_HEADER)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in _JOURNAL_HEADER})


def _write_ml_outcomes(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "v31-production"
    (root / "metrics").mkdir(parents=True)
    (root / "reports").mkdir(parents=True)
    return root


def test_not_measurable_when_stamps_sparse(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-24"
    rows = []
    for i in range(20):
        rows.append(
            {
                "Timestamp": f"{day}T10:{i:02d}:00Z",
                "DealID": f"DIAAAAXTEST{i:04d}",
                "Direction": "BUY",
                "RealizedPnL_GBP": -2.0 if i % 2 else 1.0,
                "ExitReason": "soft_loss",
                "HoldSec": "",  # sparse
                "MlScoreAtEntry": "",
            }
        )
    _write_journal(root / "metrics" / "daily_journal.csv", rows)
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["verdict"] == ReviewVerdict.NOT_MEASURABLE.value
    assert report["measurement_health"]["stamp_gate_ok"] is False
    assert report["auto_apply"] is False


def test_app_blocked_when_autopsy_app_dominates(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-24"
    rows = []
    for i in range(20):
        rows.append(
            {
                "Timestamp": f"{day}T11:{i:02d}:00Z",
                "DealID": f"DIAAAAXAPP{i:04d}",
                "Direction": "BUY",
                "RealizedPnL_GBP": 1.5 if i < 12 else -1.0,
                "ExitReason": "trail",
                "HoldSec": 200 + i,
                "MlScoreAtEntry": 0.55 + i * 0.01,
                "MarketRegime": "TREND",
            }
        )
    _write_journal(root / "metrics" / "daily_journal.csv", rows)
    autopsy = {
        "day": day,
        "summary": {
            "closes": 20,
            "winners": 12,
            "losers": 8,
            "net_gbp": 5.0,
            "by_loss_class": {"APP": 6, "LOGIC": 1, "UNKNOWN": 1},
        },
        "fundamentals_followed": {
            "verdict": "NO — APP policy breaches dominate losers",
            "app": 6,
            "logic": 1,
            "unknown": 1,
        },
    }
    (root / "reports" / f"loss_autopsy_{day}.json").write_text(
        json.dumps(autopsy), encoding="utf-8"
    )
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["verdict"] == ReviewVerdict.APP_BLOCKED.value
    assert report["loss_mix"]["app"] == 6


def test_edge_ok_clean_positive_sample(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-20"
    rows = []
    for i in range(16):
        win = i % 5 != 0  # ~80% WR
        rows.append(
            {
                "Timestamp": f"{day}T12:{i:02d}:00Z",
                "DealID": f"DIAAAAXEDGE{i:04d}",
                "Direction": "BUY",
                "RealizedPnL_GBP": 2.0 if win else -1.0,
                "ExitReason": "micro_bank",
                "HoldSec": 180 + i,
                "MlScoreAtEntry": 0.7 if win else 0.3,
                "MarketRegime": "TREND",
            }
        )
    _write_journal(root / "metrics" / "daily_journal.csv", rows)
    autopsy = {
        "day": day,
        "summary": {
            "closes": 16,
            "winners": 13,
            "losers": 3,
            "by_loss_class": {"APP": 0, "LOGIC": 3, "UNKNOWN": 0},
        },
        "fundamentals_followed": {
            "verdict": "PARTIAL",
            "app": 0,
            "logic": 3,
            "unknown": 0,
        },
    }
    (root / "reports" / f"loss_autopsy_{day}.json").write_text(
        json.dumps(autopsy), encoding="utf-8"
    )
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["verdict"] == ReviewVerdict.EDGE_OK.value
    assert report["measurement_health"]["stamp_gate_ok"] is True


def test_no_edge_measurable_but_bad(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-21"
    rows = []
    for i in range(16):
        rows.append(
            {
                "Timestamp": f"{day}T13:{i:02d}:00Z",
                "DealID": f"DIAAAAXNOE{i:04d}",
                "Direction": "SELL",
                "RealizedPnL_GBP": -2.0 if i < 12 else 0.5,
                "ExitReason": "soft_loss",
                "HoldSec": 200,
                "MlScoreAtEntry": 0.5,
                "MarketRegime": "RANGE",
            }
        )
    _write_journal(root / "metrics" / "daily_journal.csv", rows)
    autopsy = {
        "day": day,
        "summary": {
            "closes": 16,
            "losers": 12,
            "by_loss_class": {"APP": 1, "LOGIC": 10, "UNKNOWN": 1},
        },
        "fundamentals_followed": {"verdict": "LOGIC", "app": 1, "logic": 10, "unknown": 1},
    }
    (root / "reports" / f"loss_autopsy_{day}.json").write_text(
        json.dumps(autopsy), encoding="utf-8"
    )
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["verdict"] == ReviewVerdict.NO_EDGE.value


def test_write_md_json_twin(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-22"
    _write_journal(
        root / "metrics" / "daily_journal.csv",
        [
            {
                "Timestamp": f"{day}T09:00:00Z",
                "DealID": "DIAAAAXWRITE0001",
                "RealizedPnL_GBP": -1.0,
                "HoldSec": "",
                "MlScoreAtEntry": "",
            }
        ],
    )
    md_path, json_path, report = write_ml_strategy_review(day=day, data_root=root)
    assert md_path.is_file()
    assert json_path is not None and json_path.is_file()
    assert "Verdict" in md_path.read_text(encoding="utf-8")
    twin = json.loads(json_path.read_text(encoding="utf-8"))
    assert twin["verdict"] == report["verdict"]


def test_ml_outcomes_enrich_hold(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-23"
    _write_journal(
        root / "metrics" / "daily_journal.csv",
        [
            {
                "Timestamp": f"{day}T08:00:00Z",
                "DealID": "DIAAAAXENRICH001",
                "RealizedPnL_GBP": -3.0,
                "HoldSec": "",
                "MlScoreAtEntry": "",
            }
        ],
    )
    from datetime import datetime, timezone

    ts = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc).timestamp()
    _write_ml_outcomes(
        root / "metrics" / "ml_trade_outcomes.jsonl",
        [
            {
                "deal_id": "DIAAAAXENRICH001",
                "ts": ts,
                "hold_sec": 45.0,
                "ml_score": 0.62,
                "pnl": -3.0,
            }
        ],
    )
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["measurement_health"]["hold_stamped"] >= 1
    assert report["measurement_health"]["ml_stamped"] >= 1


def test_ml_lift_reports_calibration_and_excludes_invalid_scores() -> None:
    closes = []
    for i in range(20):
        high = i >= 10
        closes.append(
            {
                "ml_score": 0.8 if high else 0.2,
                "pnl_gbp": 2.0 if (high or i in (0, 1)) else -1.0,
            }
        )
    closes.append({"ml_score": 1.2, "pnl_gbp": 3.0})

    lift = compute_ml_lift(closes, improvement=None)

    assert lift.scored_n == 20
    assert lift.invalid_score_n == 1
    assert lift.calibration_status == "ok"
    assert lift.lift_high_minus_low_wr == pytest.approx(80.0)
    assert lift.brier_score is not None
    assert lift.expected_calibration_error is not None
    assert lift.buckets[0]["mean_score"] == pytest.approx(0.2)
    assert lift.buckets[-1]["observed_win_rate"] == pytest.approx(1.0)


def test_veto_regret_insufficient_without_counterfactual_labels() -> None:
    result = compute_veto_regret(
        [
            {
                "mode": "profit_veto",
                "veto": True,
                "pnl": 4.0,  # taken-trade PnL must not be misused as counterfactual
            }
        ]
    )
    assert result.status == "insufficient_data"
    assert result.available is False
    assert result.veto_events == 1
    assert result.matched_counterfactuals == 0


def test_veto_regret_uses_only_labelled_counterfactuals() -> None:
    result = compute_veto_regret(
        [
            {"mode": "profit_veto", "veto": True, "counterfactual_pnl": 3.0},
            {
                "setup_memory": {"veto": True},
                "shadow_pnl": -2.0,
            },
            {"veto_policy": "profit_veto", "pnl_if_taken": 0.0},
        ]
    )
    assert result.status == "ok"
    assert result.available is True
    assert result.matched_counterfactuals == 3
    assert result.regretted_vetoes == 1
    assert result.avoided_losses == 1
    assert result.flat_counterfactuals == 1
    assert result.counterfactual_net_gbp == pytest.approx(1.0)


def test_decide_verdict_priority() -> None:
    health_bad = MeasurementHealth(closes=10, stamp_gate_ok=False, notes=["sparse"])
    edge = StrategyEdge(n=10, wins=8, wr_pct=80.0, expectancy_gbp=1.0)
    lift = MlLift(scored_n=10, lift_positive=True)
    loss = LossMix(app=8, logic=1, unknown=0, losers=9, app_share=0.8, available=True)
    v, _ = decide_verdict(health_bad, edge, lift, loss)
    assert v == ReviewVerdict.NOT_MEASURABLE

    health_ok = MeasurementHealth(
        closes=20, hold_stamp_pct=0.9, ml_stamp_pct=0.9, clean_closes=16, stamp_gate_ok=True
    )
    v2, _ = decide_verdict(health_ok, edge, lift, loss)
    assert v2 == ReviewVerdict.APP_BLOCKED


def test_contaminated_improvement_flagged(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    day = "2026-07-24"
    _write_journal(
        root / "metrics" / "daily_journal.csv",
        [
            {
                "Timestamp": f"{day}T01:00:00Z",
                "DealID": "DIAAAAXCONT0001",
                "RealizedPnL_GBP": 0.0,
                "HoldSec": "",
            }
        ],
    )
    (root / "strategy_improvement.json").write_text(
        json.dumps(
            {
                "strategy_epoch": "init",
                "closes": [
                    {"pnl_gbp": 0.0, "won": False, "hold_sec": None},
                    {"pnl_gbp": -1.0, "won": False, "hold_sec": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = build_ml_strategy_review(day=day, data_root=root)
    assert report["strategy_edge"]["contaminated_improvement"] is True


def test_strategy_improvement_state_path_uses_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime import strategy_improvement_tracker as sit

    sit.reset_strategy_improvement_for_tests()
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(tmp_path))
    path = sit._state_path()
    assert path == tmp_path / "strategy_improvement.json"
    sit.record_managed_close(epic="IX.D.DOW.IFM.IP", pnl_gbp=1.0, exit_reason="test")
    assert path.is_file()
    sit.reset_strategy_improvement_for_tests()
