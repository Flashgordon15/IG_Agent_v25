"""Per-contract asset normalization — spread caps, pip scale, soft-loss multipliers.

Authoritative lookup for multi-market hot-path gates. Replaces DOW-only hardcoded
``max_spread_pts=3.0`` branches with epic-aware profiles consumed by RuntimeContext,
dual-core channel health, IOC maxSlippage, and entry spread vetoes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EPIC_DOW = "IX.D.DOW.IFM.IP"
EPIC_FTSE = "IX.D.FTSE.IFM.IP"
EPIC_GOLD = "CS.D.CFPGOLD.CFP.IP"
EPIC_EURUSD = "CS.D.EURUSD.CFD.IP"

ALIAS_FTSE = "UK100"
ALIAS_GOLD = "GC"
ALIAS_EURUSD = "EURUSD"


@dataclass(frozen=True, slots=True)
class ContractAssetProfile:
    key: str
    epic: str
    max_spread_pts: float
    point_multiplier: float
    is_forex: bool
    soft_loss_contract_mult: float = 1.0
    obi_threshold: float = 0.22
    trail_noise_pts: float = 1.0

    def spread_points(self, spread: float) -> float:
        try:
            s = float(spread)
        except (TypeError, ValueError):
            return float("inf")
        if self.is_forex:
            return s * max(float(self.point_multiplier), 1.0)
        return s

    def spread_allowed(self, spread: float) -> bool:
        return self.spread_points(spread) <= float(self.max_spread_pts)

    def soft_loss_scale(self, base_gbp: float) -> float:
        return max(0.25, float(base_gbp) * float(self.soft_loss_contract_mult))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "epic": self.epic,
            "max_spread_pts": self.max_spread_pts,
            "point_multiplier": self.point_multiplier,
            "is_forex": self.is_forex,
            "soft_loss_contract_mult": self.soft_loss_contract_mult,
            "obi_threshold": self.obi_threshold,
            "trail_noise_pts": self.trail_noise_pts,
        }


def _canonical_profiles() -> dict[str, ContractAssetProfile]:
    dow = ContractAssetProfile(
        key=EPIC_DOW,
        epic=EPIC_DOW,
        max_spread_pts=3.0,
        point_multiplier=1.0,
        is_forex=False,
        soft_loss_contract_mult=1.0,
        obi_threshold=0.22,
        trail_noise_pts=1.0,
    )
    ftse = ContractAssetProfile(
        key=ALIAS_FTSE,
        epic=EPIC_FTSE,
        max_spread_pts=4.5,
        point_multiplier=2.0,
        is_forex=False,
        soft_loss_contract_mult=1.15,
        obi_threshold=0.22,
        trail_noise_pts=1.5,
    )
    gold = ContractAssetProfile(
        key=ALIAS_GOLD,
        epic=EPIC_GOLD,
        max_spread_pts=40.0,
        point_multiplier=10.0,
        is_forex=False,
        soft_loss_contract_mult=1.25,
        obi_threshold=0.28,
        trail_noise_pts=4.0,
    )
    eurusd = ContractAssetProfile(
        key=ALIAS_EURUSD,
        epic=EPIC_EURUSD,
        max_spread_pts=2.0,
        point_multiplier=10000.0,
        is_forex=True,
        soft_loss_contract_mult=0.85,
        obi_threshold=0.20,
        trail_noise_pts=0.5,
    )
    out: dict[str, ContractAssetProfile] = {}
    for prof in (dow, ftse, gold, eurusd):
        out[prof.key.upper()] = prof
        out[prof.epic.upper()] = prof
    out["WALLSTREET"] = dow
    out["FTSE"] = ftse
    out["GOLD"] = gold
    out["EUR/USD"] = eurusd
    return out


_PROFILES = _canonical_profiles()


class ContractAssetNormalizer:
    """Epic/symbol → contract profile authority for spread and sizing adapters."""

    __slots__ = ("_profiles",)

    def __init__(self, profiles: dict[str, ContractAssetProfile] | None = None) -> None:
        self._profiles = profiles if profiles is not None else _canonical_profiles()

    def profile_for(self, epic: str | None) -> ContractAssetProfile:
        key = str(epic or "").strip().upper()
        if key and key in self._profiles:
            return self._profiles[key]
        for token, alias in (
            ("DOW", EPIC_DOW),
            ("FTSE", EPIC_FTSE),
            ("CFPGOLD", EPIC_GOLD),
            ("GOLD", EPIC_GOLD),
            ("EURUSD", EPIC_EURUSD),
        ):
            if token in key:
                return self._profiles[alias.upper()]
        return self._profiles[EPIC_DOW.upper()]

    def max_spread_pts(self, epic: str | None, cfg: Any | None = None) -> float:
        key = str(epic or "").strip()
        if cfg is not None and hasattr(cfg, "get"):
            try:
                markets = cfg.get("markets") or {}
                if isinstance(markets, dict):
                    for row in markets.values():
                        if isinstance(row, dict) and str(row.get("epic") or "") == key:
                            raw = row.get("max_spread_pts")
                            if raw is not None:
                                return float(raw)
            except Exception:
                pass
        return float(self.profile_for(key).max_spread_pts)

    def spread_points(self, epic: str | None, spread: float) -> float:
        return self.profile_for(epic).spread_points(spread)

    def spread_allowed(self, epic: str | None, spread: float) -> bool:
        return self.profile_for(epic).spread_allowed(spread)

    def adapt_max_slippage(self, epic: str | None, max_slippage: int | float) -> int | float:
        prof = self.profile_for(epic)
        if prof.is_forex:
            return max(0.1, round(float(max_slippage), 1))
        return max(1, int(round(float(max_slippage))))

    def compute_max_slippage(
        self,
        epic: str | None,
        bid: float,
        offer: float,
        *,
        slip_mult: float = 0.5,
    ) -> int | float:
        spread = float(offer) - float(bid)
        if spread <= 0:
            return 1
        mult = float(slip_mult) if float(slip_mult) > 0 else 0.5
        raw = spread * mult
        prof = self.profile_for(epic)
        if prof.is_forex:
            pips = raw * max(float(prof.point_multiplier), 1.0)
            return max(0.1, round(float(pips), 1))
        return max(1, int(round(raw)))

    def profiles_snapshot(self) -> dict[str, Any]:
        canon = {}
        for epic in (EPIC_DOW, EPIC_FTSE, EPIC_GOLD, EPIC_EURUSD):
            canon[epic] = self._profiles[epic.upper()].to_dict()
        return {"profiles": canon}

    def reset_for_tests(self) -> None:
        self._profiles.clear()
        self._profiles.update(_canonical_profiles())


_NORMALIZER: ContractAssetNormalizer | None = None


def get_contract_asset_normalizer() -> ContractAssetNormalizer:
    global _NORMALIZER
    if _NORMALIZER is None:
        _NORMALIZER = ContractAssetNormalizer()
    return _NORMALIZER


def reset_contract_asset_normalizer_for_tests() -> None:
    get_contract_asset_normalizer().reset_for_tests()


def resolve_contract_profile(epic: str | None) -> ContractAssetProfile:
    return get_contract_asset_normalizer().profile_for(epic)


def resolve_max_spread_pts(epic: str | None, cfg: Any | None = None) -> float:
    return get_contract_asset_normalizer().max_spread_pts(epic, cfg)
