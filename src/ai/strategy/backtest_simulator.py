"""Out-of-sample backtest skeleton — 70/30 IS/OOS split (§18.5)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai.paths import strategy_proposals_path
from ai.strategy.performance_reviewer import build_friction_matrix, friction_warning


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_strategy_proposals(path: Path | None = None) -> dict[str, Any]:
    p = path or strategy_proposals_path()
    if not p.exists():
        return {"proposals": [], "last_updated": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("proposals", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"proposals": [], "last_updated": None}


def save_strategy_proposals(store: dict[str, Any], path: Path | None = None) -> Path:
    p = path or strategy_proposals_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    store["last_updated"] = _utc_now()
    p.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    return p


def split_is_oos(
    bars: list[Any], *, is_ratio: float = 0.70
) -> tuple[list[Any], list[Any]]:
    """Strict 70/30 timeline split for tuning vs evaluation."""
    if not bars:
        return [], []
    cut = max(1, int(len(bars) * is_ratio))
    if cut >= len(bars):
        cut = max(1, len(bars) - 1)
    return bars[:cut], bars[cut:]


@dataclass
class BacktestSimulator:
    """Mock OOS container — writes approved-ready results to strategy_proposals.json."""

    is_ratio: float = 0.70
    proposals_path: Path = field(default_factory=strategy_proposals_path)

    def run_mock_backtest(
        self,
        *,
        epic: str,
        proposal_name: str,
        bars: list[dict[str, Any]] | None = None,
        quotes: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        bars = bars or [{"close": 100.0 + i} for i in range(100)]
        is_bars, oos_bars = split_is_oos(bars, is_ratio=self.is_ratio)

        friction = friction_warning(epic, spread_pts=1.0, atr_pts=10.0)
        matrix = build_friction_matrix([epic], quotes=quotes or {})

        oos_metrics = {
            "wr": 0.52,
            "expectancy_gbp": 12.5,
            "profit_factor": 1.25,
            "spread_friction_pct": (
                round((friction.spread_friction_ratio or 0) * 100, 2)
                if friction.spread_friction_ratio is not None
                else None
            ),
            "max_dd_gbp": 180.0,
            "n_trades": len(oos_bars),
        }
        oos_ok = (
            not friction.prohibited
            and oos_metrics["expectancy_gbp"] > 0
            and oos_metrics["wr"] >= 0.50
            and oos_metrics["profit_factor"] >= 1.2
        )

        proposal_id = uuid.uuid4().hex[:12]
        proposal: dict[str, Any] = {
            "id": proposal_id,
            "name": proposal_name,
            "epic": epic,
            "status": "ready_for_review" if oos_ok else "rejected_oos",
            "created_at": _utc_now(),
            "split": {
                "is_ratio": self.is_ratio,
                "is_bars": len(is_bars),
                "oos_bars": len(oos_bars),
            },
            "oos_metrics": oos_metrics,
            "friction_matrix": matrix,
            "evidence_pack": {
                "replay_hash": f"mock-{proposal_id}",
                "is_bar_count": len(is_bars),
                "oos_bar_count": len(oos_bars),
                "cli": "backtest_simulator.run_mock_backtest",
            },
        }

        store = load_strategy_proposals(self.proposals_path)
        store.setdefault("proposals", []).append(proposal)
        save_strategy_proposals(store, self.proposals_path)
        return proposal

    def approve_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Mark proposal approved — triggers Operational AI validation on next poll."""
        store = load_strategy_proposals(self.proposals_path)
        for item in store.get("proposals") or []:
            if str(item.get("id")) != str(proposal_id):
                continue
            item["status"] = "approved"
            item["approved_at"] = _utc_now()
            save_strategy_proposals(store, self.proposals_path)
            return item
        return None
