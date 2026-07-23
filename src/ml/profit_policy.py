"""
Profit philosophy — entry-side policy toward 70% WR and larger winners.

Principles:
  - A win is a win: bank at target, but let runners trail before quick-bank
  - Losers fail fast: handled by micro_risk soft_loss (exit side)
  - Marginal ML probability → skip entry
  - Session on a heater → slight confidence boost; cold streak → tighten
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from system.config import Config
from system.engine_log import log_engine


@dataclass
class ProfitPolicyVerdict:
    confidence: float
    boost_pts: float = 0.0
    penalty_pts: float = 0.0
    veto: bool = False
    reason: str = "ok"


def _policy_cfg(cfg: Config) -> dict[str, Any]:
    block = cfg.get("profit_philosophy")
    return dict(block) if isinstance(block, dict) else {}


def apply_profit_policy(
    cfg: Config,
    confidence: float,
    *,
    ml_prob: float | None,
    store: Any | None = None,
) -> ProfitPolicyVerdict:
    """Post-ML confidence adjustments for win-rate and edge quality."""
    pol = _policy_cfg(cfg)
    if not pol.get("enabled", True):
        return ProfitPolicyVerdict(confidence=confidence)

    conf = float(confidence)
    boost = 0.0
    penalty = 0.0
    reasons: list[str] = []

    # Marginal ML — skip low-conviction entries (toward 70% WR)
    min_ml = float(pol.get("min_ml_probability") or 0.52)
    if ml_prob is not None and pol.get("marginal_ml_veto", True):
        if float(ml_prob) < min_ml:
            log_engine(
                f"[PROFIT POLICY] veto ml_prob={ml_prob:.3f} < {min_ml:.2f}"
            )
            return ProfitPolicyVerdict(
                confidence=0.0,
                veto=True,
                reason=f"ml_prob {ml_prob:.3f} < {min_ml:.2f}",
            )

    # Session recent WR — tighten when cold, slight boost when hot
    if store is not None and hasattr(store, "recent_confirmed_closed_trades"):
        try:
            from system.learning_trade_policy import is_ig_import_setup_key

            rows = store.recent_confirmed_closed_trades(
                int(pol.get("recent_trades_lookback") or 10)
            )
            rows = [
                r
                for r in rows
                if not is_ig_import_setup_key(str(r.get("setup_name") or r.get("setup_key") or ""))
            ]
        except Exception:
            rows = []
        if len(rows) >= int(pol.get("min_recent_for_session_adj") or 5):
            wins = sum(
                1
                for r in rows
                if str(r.get("result") or "").upper() == "WIN"
                or (
                    not r.get("result")
                    and float(r.get("ig_pnl_currency") or r.get("pnl") or 0) > 0
                )
            )
            wr = wins / len(rows)
            hot_wr = float(pol.get("session_hot_wr") or 0.65)
            cold_wr = float(pol.get("session_cold_wr") or 0.45)
            if wr >= hot_wr:
                boost = float(pol.get("session_hot_boost_pts") or 3.0)
                reasons.append(f"session_hot wr={wr:.0%}")
            elif wr < cold_wr:
                penalty = float(pol.get("session_cold_penalty_pts") or 8.0)
                reasons.append(f"session_cold wr={wr:.0%}")

    conf = max(0.0, min(100.0, conf + boost - penalty))
    return ProfitPolicyVerdict(
        confidence=conf,
        boost_pts=boost,
        penalty_pts=penalty,
        reason=" | ".join(reasons) if reasons else "ok",
    )
