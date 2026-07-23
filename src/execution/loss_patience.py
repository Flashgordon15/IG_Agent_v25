"""
Regime-aware loss patience — hold small losers for mean reversion, cut on shift.

Motivation (operator request): the desk was churning "lots of small losses".
Many of those are transient adverse excursions that drift back to the mean; a
mechanical soft-loss cut realises them as losses. This module lets the desk
*defer* the soft-loss cut for a losing position **only** when:

  1. the feature is explicitly enabled (default OFF — no live behaviour change),
  2. the signal feed for the epic is FRESH (you cannot judge drift vs shift on a
     stale quote — this is the exact failure that turned £1.68 losses into £21),
  3. the loss is still comfortably above the HARD loss cap (patience operates
     strictly inside the soft -> cap*band window; the hard cap is never removed),
  4. the position is not too old, and
  5. the live microstructure regime does NOT confirm a shift against the trade
     (i.e. momentum has not flipped decisively adverse) — a genuine regime shift
     means the small loss is real and should be cut now.

If any guard fails, ``hold`` is False and the caller cuts as normal. The hard
loss cap, max-age, and cap-breach flatten paths remain fully in force regardless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Microstructure regimes that indicate momentum has turned decisively against a
# position of the given side — i.e. the market has SHIFTED, not merely drifted.
_ADVERSE_REGIMES_FOR_LONG = {"MOMENTUM_DOWN", "SWEEP_SELL"}
_ADVERSE_REGIMES_FOR_SHORT = {"MOMENTUM_UP", "SWEEP_BUY"}


@dataclass(frozen=True)
class PatienceDecision:
    hold: bool
    reason: str
    regime: str = ""
    confidence: float = 0.0
    quote_age_sec: float | None = None


def loss_patience_cfg(cfg: Any | None) -> dict[str, Any]:
    if cfg is None:
        return {}
    try:
        block = cfg.get("loss_patience") if hasattr(cfg, "get") else getattr(
            cfg, "loss_patience", None
        )
    except Exception:
        block = None
    return dict(block) if isinstance(block, dict) else {}


def loss_patience_enabled(cfg: Any | None) -> bool:
    return bool(loss_patience_cfg(cfg).get("enabled", False))


def _quote_age_sec(epic: str) -> float | None:
    try:
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(epic)
        if snap is None or snap.bid <= 0 or snap.offer <= 0:
            return None
        return float(snap.age_seconds())
    except Exception:
        return None


def _microstructure(epic: str) -> Any | None:
    try:
        from intelligence.pipeline_bridge import get_intelligence_layer

        return get_intelligence_layer().microstructure_verdict(epic)
    except Exception:
        return None


def should_hold_losing_position(
    *,
    epic: str,
    direction: str,
    pnl_gbp: float,
    soft_loss_gbp: float,
    loss_cap_gbp: float,
    open_mins: float | None,
    cfg: Any | None,
) -> PatienceDecision:
    """Decide whether to DEFER a soft-loss cut for a losing position.

    ``hold=True`` means: skip the soft-loss flatten this tick (the hard loss cap
    still applies). ``hold=False`` means: cut as normal. Conservative by default
    — any uncertainty (feature off, stale feed, missing regime) yields cut.
    """
    fq = loss_patience_cfg(cfg)
    if not fq.get("enabled", False):
        return PatienceDecision(hold=False, reason="disabled")

    pnl = float(pnl_gbp)
    soft = abs(float(soft_loss_gbp))
    cap = abs(float(loss_cap_gbp))

    # Only applies to a position past soft loss but not near the hard cap.
    if pnl > -soft:
        return PatienceDecision(hold=False, reason="not_in_loss_band")

    band_ratio = float(fq.get("hold_band_ratio", 0.85) or 0.85)
    if cap > 0 and pnl <= -(cap * band_ratio):
        return PatienceDecision(
            hold=False, reason=f"near_hard_cap pnl={pnl:.2f} cap={cap:.2f}"
        )

    max_hold_min = float(fq.get("max_hold_minutes", 20.0) or 20.0)
    max_soft_sec = fq.get("max_hold_soft_loss_sec")
    if open_mins is not None:
        open_secs = float(open_mins) * 60.0
        if max_soft_sec is not None and float(max_soft_sec) > 0:
            if open_secs >= float(max_soft_sec):
                return PatienceDecision(
                    hold=False,
                    reason=(
                        f"max_soft_loss_hold {open_secs:.0f}s>="
                        f"{float(max_soft_sec):.0f}s"
                    ),
                )
        elif max_hold_min > 0 and float(open_mins) > max_hold_min:
            return PatienceDecision(
                hold=False, reason=f"max_hold {open_mins:.0f}m>{max_hold_min:.0f}m"
            )

    # Feed must be fresh — drift vs shift is undecidable on a stale quote.
    max_age = float(fq.get("max_quote_age_sec", 15.0) or 15.0)
    age = _quote_age_sec(epic)
    if age is None:
        return PatienceDecision(hold=False, reason="no_quote")
    if age > max_age:
        return PatienceDecision(
            hold=False,
            reason=f"stale_feed age={age:.0f}s>{max_age:.0f}s",
            quote_age_sec=age,
        )

    # Regime: has momentum turned decisively against the position?
    verdict = _microstructure(epic)
    if verdict is None:
        return PatienceDecision(
            hold=False, reason="no_regime", quote_age_sec=age
        )

    regime = str(getattr(verdict, "regime", "") or "").upper()
    confidence = float(getattr(verdict, "confidence", 0.0) or 0.0)
    mom_5m = float(getattr(verdict, "momentum_5m", 0.0) or 0.0)
    side = str(direction or "").upper()

    adverse_set = (
        _ADVERSE_REGIMES_FOR_LONG
        if side in ("BUY", "LONG")
        else _ADVERSE_REGIMES_FOR_SHORT
    )
    cut_conf = float(fq.get("regime_shift_confidence", 0.55) or 0.55)
    if regime in adverse_set and confidence >= cut_conf:
        return PatienceDecision(
            hold=False,
            reason=f"regime_shift {regime} conf={confidence:.2f} — cut small loss",
            regime=regime,
            confidence=confidence,
            quote_age_sec=age,
        )

    # Optional secondary confirmation on raw 5m momentum sign/size.
    adverse_mom = float(fq.get("adverse_momentum_5m", 0.0) or 0.0)
    if adverse_mom > 0:
        against = (side in ("BUY", "LONG") and mom_5m < -adverse_mom) or (
            side in ("SELL", "SHORT") and mom_5m > adverse_mom
        )
        if against:
            return PatienceDecision(
                hold=False,
                reason=f"momentum_shift mom5m={mom_5m:.6f} — cut",
                regime=regime,
                confidence=confidence,
                quote_age_sec=age,
            )

    # Regime supports reversion (or is neutral/ranging) — hold for the drift back.
    return PatienceDecision(
        hold=True,
        reason=f"mean_reversion_hold regime={regime or 'NEUTRAL'} conf={confidence:.2f}",
        regime=regime,
        confidence=confidence,
        quote_age_sec=age,
    )
