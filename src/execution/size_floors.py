"""Hard IG minimum deal sizes — authoritative floors even when REST constraints fail."""

from __future__ import annotations

from typing import Any

# IG spreadbet DEMO min £/point (verified 2026-07-02 via GET /markets/{epic}).
SPREADBET_MIN_DEAL_BY_EPIC: dict[str, float] = {
    "CS.D.CFPGOLD.CFP.IP": 10.0,
    "IX.D.NIKKEI.IFM.IP": 0.5,
    "IX.D.DOW.IFM.IP": 0.5,
    "IX.D.DAX.IFM.IP": 0.5,
    "IX.D.FTSE.IFM.IP": 0.5,
    "IX.D.NASDAQ.IFM.IP": 0.5,
    "CS.D.EURUSD.CFD.IP": 1.0,
    "CS.D.EURUSD.TODAY.IP": 1.0,
    "CS.D.GBPUSD.CFD.IP": 1.0,
    "CS.D.GBPUSD.TODAY.IP": 1.0,
}

# CFD / integer-lot path defaults when epic not listed.
CFD_MIN_DEAL_BY_EPIC: dict[str, float] = {
    "CS.D.CFPGOLD.CFP.IP": 1.0,
    "IX.D.NIKKEI.IFM.IP": 1.0,
    "IX.D.DOW.IFM.IP": 1.0,
    "CS.D.EURUSD.CFD.IP": 1.0,
    "CS.D.GBPUSD.CFD.IP": 1.0,
}

# Substring fallbacks (upper-case epic tokens).
_SPREADBET_PATTERN_FLOORS: tuple[tuple[str, float], ...] = (
    ("CFPGOLD", 10.0),
    ("GOLD", 10.0),
    ("NIKKEI", 0.5),
    ("DOW", 0.5),
    ("DAX", 0.5),
    ("FTSE", 0.5),
    ("NASDAQ", 0.5),
    ("EURUSD", 1.0),
    ("GBPUSD", 1.0),
)

_CFD_PATTERN_FLOORS: tuple[tuple[str, float], ...] = (
    ("CFPGOLD", 1.0),
    ("GOLD", 1.0),
    ("NIKKEI", 1.0),
    ("DOW", 1.0),
    ("EURUSD", 1.0),
    ("GBPUSD", 1.0),
)

SPREADBET_DEFAULT_MIN_DEAL = 0.5
CFD_DEFAULT_MIN_DEAL = 1.0


def _spreadbet_product(cfg: Any | None = None) -> bool:
    try:
        from execution.ig_size_validator import fractional_lot_execution_enabled

        if fractional_lot_execution_enabled(cfg):
            return True
    except Exception:
        pass
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return False
    try:
        dual = cfg.get("dual_core") if hasattr(cfg, "get") else getattr(cfg, "dual_core", {})
        if isinstance(dual, dict) and str(dual.get("broker_account_product", "")).upper() == "SPREADBET":
            return True
    except Exception:
        pass
    return False


def _pattern_floor(epic: str, patterns: tuple[tuple[str, float], ...]) -> float:
    key = str(epic or "").upper()
    for token, floor in patterns:
        if token in key:
            return float(floor)
    return 0.0


def hard_min_deal_size(epic: str, *, cfg: Any | None = None) -> float:
    """
    Hard-coded IG minimum deal size for *epic*.

    Used when REST constraint fetch fails and as a floor on top of live IG rules.
    """
    key = str(epic or "").strip()
    if not key:
        return 0.0
    try:
        from trading.micro_lot_verification import (
            micro_contract_size,
            micro_lot_verification_enabled,
        )

        if micro_lot_verification_enabled():
            return float(micro_contract_size())
    except Exception:
        pass
    if _spreadbet_product(cfg):
        if key in SPREADBET_MIN_DEAL_BY_EPIC:
            return float(SPREADBET_MIN_DEAL_BY_EPIC[key])
        pat = _pattern_floor(key, _SPREADBET_PATTERN_FLOORS)
        return pat if pat > 0 else SPREADBET_DEFAULT_MIN_DEAL
    if key in CFD_MIN_DEAL_BY_EPIC:
        return float(CFD_MIN_DEAL_BY_EPIC[key])
    pat = _pattern_floor(key, _CFD_PATTERN_FLOORS)
    return pat if pat > 0 else CFD_DEFAULT_MIN_DEAL


def effective_min_deal_size(
    epic: str,
    *,
    cfg: Any | None = None,
    rest_min: float = 0.0,
) -> float:
    """max(hard floor, live IG min from REST)."""
    hard = hard_min_deal_size(epic, cfg=cfg)
    live = max(0.0, float(rest_min or 0.0))
    return max(hard, live)


# Backward-compatible alias used by gate execution paths.
EPIC_OPERATIONAL_SIZE_FLOORS: dict[str, float] = dict(SPREADBET_MIN_DEAL_BY_EPIC)


def operational_size_floor(epic: str, *, cfg: Any | None = None) -> float:
    """Return hard minimum deal size for epic."""
    return hard_min_deal_size(epic, cfg=cfg)


def apply_operational_size_floor(size: float, epic: str, *, cfg: Any | None = None) -> float:
    """Raise size to epic hard minimum, then weld to IG two-decimal lot contract."""
    from trading.position_ladder import apply_broker_lot_contract

    floor = hard_min_deal_size(epic, cfg=cfg)
    raised = max(float(size), floor) if floor > 0 else float(size)
    return apply_broker_lot_contract(raised, epic)
