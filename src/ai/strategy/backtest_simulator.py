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


_POINT_VALUE_GBP = 1.0
_EMA_FAST = 12
_EMA_SLOW = 26
_ATR_PERIOD = 14
_STOP_ATR = 1.5
_TP_ATR = 2.0
_PF_CAP = 99.0


def _bar_field(bar: Any, *names: str) -> float | None:
    if not isinstance(bar, dict):
        return None
    for name in names:
        val = bar.get(name)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _ema_series(closes: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    ema = closes[0]
    for c in closes:
        ema = ema + alpha * (c - ema)
        out.append(ema)
    return out


def _atr_series(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> list[float]:
    out: list[float] = []
    atr = 0.0
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            prev_c = closes[i - 1]
            tr = max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c))
        atr = tr if i == 0 else atr + (tr - atr) / period
        out.append(atr)
    return out


def simulate_oos_trades(
    bars: list[dict[str, Any]], oos_start: int
) -> dict[str, Any]:
    """EMA(12/26) crossover long/short with ATR(14) stop/TP, OOS fills only.

    Signals at bar i use only bars <= i; fills occur at bar i+1 open
    (close mid when no open field).  Returns wr / expectancy_gbp /
    profit_factor / max_dd_gbp / n_trades.
    """
    zero = {
        "wr": 0.0,
        "expectancy_gbp": 0.0,
        "profit_factor": 0.0,
        "max_dd_gbp": 0.0,
        "n_trades": 0,
    }
    n = len(bars)
    if n < 2 or oos_start >= n:
        return zero

    closes: list[float] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for bar in bars:
        c = _bar_field(bar, "close", "c", "mid")
        if c is None:
            c = closes[-1] if closes else 0.0
        o = _bar_field(bar, "open", "o")
        h = _bar_field(bar, "high", "h")
        low = _bar_field(bar, "low", "l")
        closes.append(c)
        opens.append(o if o is not None else c)
        highs.append(h if h is not None else c)
        lows.append(low if low is not None else c)

    ema_fast = _ema_series(closes, _EMA_FAST)
    ema_slow = _ema_series(closes, _EMA_SLOW)
    atr = _atr_series(highs, lows, closes, _ATR_PERIOD)

    pnls: list[float] = []
    position: dict[str, float] | None = None

    for j in range(max(1, oos_start), n):
        sig = j - 1
        direction = 0
        if ema_fast[sig] > ema_slow[sig]:
            direction = 1
        elif ema_fast[sig] < ema_slow[sig]:
            direction = -1

        if position is not None and direction != 0 and direction != position["dir"]:
            pnls.append((opens[j] - position["entry"]) * position["dir"])
            position = None

        if position is None and direction != 0 and atr[sig] > 0:
            entry = opens[j]
            position = {
                "dir": float(direction),
                "entry": entry,
                "stop": entry - _STOP_ATR * atr[sig] * direction,
                "tp": entry + _TP_ATR * atr[sig] * direction,
            }
            continue

        if position is not None:
            d = position["dir"]
            hit_stop = lows[j] <= position["stop"] if d > 0 else highs[j] >= position["stop"]
            hit_tp = highs[j] >= position["tp"] if d > 0 else lows[j] <= position["tp"]
            if hit_stop:
                pnls.append((position["stop"] - position["entry"]) * d)
                position = None
            elif hit_tp:
                pnls.append((position["tp"] - position["entry"]) * d)
                position = None

    if position is not None:
        pnls.append((closes[-1] - position["entry"]) * position["dir"])

    if not pnls:
        return zero

    pnls_gbp = [p * _POINT_VALUE_GBP for p in pnls]
    wins = sum(1 for p in pnls_gbp if p > 0)
    gross_win = sum(p for p in pnls_gbp if p > 0)
    gross_loss = -sum(p for p in pnls_gbp if p < 0)
    if gross_loss > 0:
        pf = min(gross_win / gross_loss, _PF_CAP)
    else:
        pf = _PF_CAP if gross_win > 0 else 0.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls_gbp:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "wr": round(wins / len(pnls_gbp), 4),
        "expectancy_gbp": round(sum(pnls_gbp) / len(pnls_gbp), 4),
        "profit_factor": round(pf, 4),
        "max_dd_gbp": round(max_dd, 4),
        "n_trades": len(pnls_gbp),
    }


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

        sim = simulate_oos_trades(bars, oos_start=len(is_bars))
        oos_metrics = {
            "wr": sim["wr"],
            "expectancy_gbp": sim["expectancy_gbp"],
            "profit_factor": sim["profit_factor"],
            "spread_friction_pct": (
                round((friction.spread_friction_ratio or 0) * 100, 2)
                if friction.spread_friction_ratio is not None
                else None
            ),
            "max_dd_gbp": sim["max_dd_gbp"],
            "n_trades": sim["n_trades"],
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
