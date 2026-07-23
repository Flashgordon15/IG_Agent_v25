"""Tiered profit banking — bank micro/mid wins before they round-trip to soft_loss."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfitBankTier:
    peak_min_gbp: float
    bank_floor_gbp: float
    fade_ratio: float
    label: str


_DEFAULT_TIERS: tuple[ProfitBankTier, ...] = (
    ProfitBankTier(0.80, 0.60, 0.55, "micro_bank"),
    ProfitBankTier(2.50, 2.00, 0.68, "mid_bank"),
    ProfitBankTier(5.00, 4.00, 0.75, "solid_bank"),
)


def load_profit_bank_tiers(cfg: Any | None) -> tuple[ProfitBankTier, ...]:
    if cfg is None:
        return _DEFAULT_TIERS
    try:
        mr = getattr(cfg, "micro_risk", None) or (
            cfg.get("micro_risk") if hasattr(cfg, "get") else None
        )
        if not isinstance(mr, dict):
            return _DEFAULT_TIERS
        raw = mr.get("tiered_profit_banks")
        if not isinstance(raw, list) or not raw:
            return _DEFAULT_TIERS
        tiers: list[ProfitBankTier] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            tiers.append(
                ProfitBankTier(
                    peak_min_gbp=float(row.get("peak_min_gbp") or 0),
                    bank_floor_gbp=float(row.get("bank_floor_gbp") or 0),
                    fade_ratio=float(row.get("fade_ratio") or 0.6),
                    label=str(row.get("label") or "tier_bank"),
                )
            )
        return tuple(sorted(tiers, key=lambda t: t.peak_min_gbp)) or _DEFAULT_TIERS
    except Exception:
        return _DEFAULT_TIERS


def tiered_bank_reason(
    *,
    peak: float,
    pnl: float,
    trail_trigger_gbp: float,
    tiers: tuple[ProfitBankTier, ...] | None = None,
    cfg: Any | None = None,
) -> str | None:
    """
    Return flatten reason when a tier bank fires, else None.

    Only applies below trail_trigger — above that the trail floor owns exits.
    """
    if peak <= 0 or pnl <= 0:
        return None
    trigger = float(trail_trigger_gbp or 0)
    if trigger > 0 and peak >= trigger:
        return None

    bank_tiers = tiers or load_profit_bank_tiers(cfg)
    for tier in reversed(bank_tiers):
        if peak < tier.peak_min_gbp:
            continue
        fade_level = peak * tier.fade_ratio
        if pnl <= fade_level and pnl >= tier.bank_floor_gbp:
            return (
                f"{tier.label} pnl={pnl:.2f} peak={peak:.2f} "
                f"fade<={fade_level:.2f} floor>={tier.bank_floor_gbp:.2f}"
            )
    return None
