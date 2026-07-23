"""In-memory Shared Memory Context Matrix — zero disk I/O on the hot path.

Thread-safe RAM authority for verified open positions and trailing floors.
Hollow ghost rows (entry<=0 or pnl_gbp is None without a critical alarm) are
hard-vetoed before they can reach the GUI or sniper arming path.

``RuntimeContext`` holds slotted per-EPIC metric overrides (spread caps,
point multipliers, forex flags) used by entry gates and IOC payload adapters.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Default display budget — overridden at runtime by transport-aware helper.
QUOTE_FRESHNESS_SEC = 0.5
ATR_TP_MULT = 3.5

# Canonical IG epics (spreadbet demo) + operator short aliases.
EPIC_DOW = "IX.D.DOW.IFM.IP"
EPIC_FTSE = "IX.D.FTSE.IFM.IP"
EPIC_GOLD = "CS.D.CFPGOLD.CFP.IP"
EPIC_EURUSD = "CS.D.EURUSD.CFD.IP"

# Alias keys requested by multi-market expansion blueprint.
ALIAS_FTSE = "UK100"
ALIAS_GOLD = "GC"
ALIAS_EURUSD = "EURUSD"


class AssetMetricProfile:
    """Per-asset hot-path metric overrides — dense ``__slots__`` packing."""

    __slots__ = (
        "key",
        "epic",
        "max_spread_pts",
        "point_multiplier",
        "is_forex",
        "obi_threshold",
        "trail_noise_pts",
    )

    def __init__(
        self,
        *,
        key: str,
        epic: str,
        max_spread_pts: float,
        point_multiplier: float,
        is_forex: bool,
        obi_threshold: float = 0.15,
        trail_noise_pts: float = 1.0,
    ) -> None:
        self.key = str(key)
        self.epic = str(epic)
        self.max_spread_pts = float(max_spread_pts)
        self.point_multiplier = float(point_multiplier)
        self.is_forex = bool(is_forex)
        self.obi_threshold = float(obi_threshold)
        self.trail_noise_pts = float(trail_noise_pts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "epic": self.epic,
            "max_spread_pts": self.max_spread_pts,
            "point_multiplier": self.point_multiplier,
            "is_forex": self.is_forex,
            "obi_threshold": self.obi_threshold,
            "trail_noise_pts": self.trail_noise_pts,
        }


def _default_asset_profiles() -> dict[str, AssetMetricProfile]:
    """Build canonical + alias profile map from ContractAssetNormalizer."""
    from execution.contract_asset_normalizer import get_contract_asset_normalizer

    out: dict[str, AssetMetricProfile] = {}
    normalizer = get_contract_asset_normalizer()
    for epic in (EPIC_DOW, EPIC_FTSE, EPIC_GOLD, EPIC_EURUSD):
        cp = normalizer.profile_for(epic)
        prof = AssetMetricProfile(
            key=cp.key,
            epic=cp.epic,
            max_spread_pts=cp.max_spread_pts,
            point_multiplier=cp.point_multiplier,
            is_forex=cp.is_forex,
            obi_threshold=cp.obi_threshold,
            trail_noise_pts=cp.trail_noise_pts,
        )
        out[prof.key.upper()] = prof
        out[prof.epic.upper()] = prof
    dow = out[EPIC_DOW.upper()]
    out["WALLSTREET"] = dow
    out["FTSE"] = out[EPIC_FTSE.upper()]
    out["GOLD"] = out[EPIC_GOLD.upper()]
    out["EUR/USD"] = out[EPIC_EURUSD.upper()]
    return out


class RuntimeContext:
    """Slotted multi-asset memory matrix — active EPIC metric authority."""

    __slots__ = ("_lock", "_profiles", "_active_epic")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles = _default_asset_profiles()
        self._active_epic = EPIC_DOW

    @property
    def active_epic(self) -> str:
        with self._lock:
            return self._active_epic

    @property
    def active_asset(self) -> AssetMetricProfile:
        with self._lock:
            return self.profile_for(self._active_epic)

    def set_active_epic(self, epic: str) -> AssetMetricProfile:
        prof = self.profile_for(epic)
        with self._lock:
            self._active_epic = prof.epic
        return prof

    def profile_for(self, epic: str | None) -> AssetMetricProfile:
        from execution.contract_asset_normalizer import resolve_contract_profile

        cp = resolve_contract_profile(epic)
        key = cp.key.upper()
        with self._lock:
            if key in self._profiles:
                return self._profiles[key]
            if cp.epic.upper() in self._profiles:
                return self._profiles[cp.epic.upper()]
            prof = AssetMetricProfile(
                key=cp.key,
                epic=cp.epic,
                max_spread_pts=cp.max_spread_pts,
                point_multiplier=cp.point_multiplier,
                is_forex=cp.is_forex,
                obi_threshold=cp.obi_threshold,
                trail_noise_pts=cp.trail_noise_pts,
            )
            self._profiles[key] = prof
            self._profiles[cp.epic.upper()] = prof
            return prof

    def spread_points(self, epic: str, spread: float) -> float:
        """Normalize raw price spread into profile units (index pts or FX pips)."""
        try:
            s = float(spread)
        except (TypeError, ValueError):
            return float("inf")
        prof = self.profile_for(epic)
        if prof.is_forex:
            return s * max(float(prof.point_multiplier), 1.0)
        return s

    def spread_allowed(self, epic: str, spread: float) -> bool:
        """True when normalized spread ≤ ``active_asset.max_spread_pts`` for *epic*.

        Indices/commodities: absolute price points.
        Forex: price spread × ``point_multiplier`` → pips vs ``max_spread_pts``.
        """
        pts = self.spread_points(epic, spread)
        return pts <= float(self.profile_for(epic).max_spread_pts)

    def trail_price_delta(self, epic: str, trail_pts: float) -> float:
        """Convert trail steps into price units using contract point multiplier."""
        prof = self.profile_for(epic)
        pts = float(trail_pts)
        if prof.is_forex:
            # point_multiplier = pip scale (10000 for EURUSD) → price = pips / 10000
            return pts / max(prof.point_multiplier, 1.0)
        # Index/commodity: one trail step = ``point_multiplier`` price points
        return pts * max(prof.point_multiplier, 1.0)

    def trail_gbp(
        self, epic: str, trail_pts: float, size: float
    ) -> float:
        """GBP move for *trail_pts* at transmit *size* (£/pt spreadbet)."""
        price_delta = self.trail_price_delta(epic, trail_pts)
        # For forex, size is £/pip-equivalent; price_delta already in price units.
        # GBP ≈ price_delta × size × pip_scale for FX; for indices GBP ≈ pts × size.
        prof = self.profile_for(epic)
        if prof.is_forex:
            return abs(price_delta) * abs(float(size)) * prof.point_multiplier
        return abs(float(trail_pts)) * abs(float(size))

    def profiles_snapshot(self) -> dict[str, Any]:
        with self._lock:
            canon = {}
            for epic in (EPIC_DOW, EPIC_FTSE, EPIC_GOLD, EPIC_EURUSD):
                canon[epic] = self._profiles[epic.upper()].to_dict()
            return {
                "active_epic": self._active_epic,
                "active_asset": self.profile_for(self._active_epic).to_dict(),
                "profiles": canon,
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._profiles = _default_asset_profiles()
            self._active_epic = EPIC_DOW


_RUNTIME: RuntimeContext | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime_context() -> RuntimeContext:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = RuntimeContext()
        return _RUNTIME


def reset_runtime_context_for_tests() -> None:
    get_runtime_context().reset_for_tests()


def resolve_asset_profile(epic: str | None) -> AssetMetricProfile:
    return get_runtime_context().profile_for(epic)


def _active_quote_budget_sec(budget_sec: float | None = None) -> float:
    if budget_sec is not None and float(budget_sec) > 0:
        return float(budget_sec)
    try:
        from system.market_integrity import effective_entry_quote_budget_sec

        return float(effective_entry_quote_budget_sec())
    except Exception:
        return float(QUOTE_FRESHNESS_SEC)


class OpenPositionMem:
    """Verified open position — ``__slots__`` for dense RAM packing."""

    __slots__ = (
        "deal_id",
        "epic",
        "direction",
        "size",
        "entry",
        "pnl_gbp",
        "soft_loss_gbp",
        "trail_floor_gbp",
        "target_gbp",
        "peak_profit_gbp",
        "atr",
        "take_profit_level",
        "source",
        "updated_at",
    )

    def __init__(
        self,
        *,
        deal_id: str,
        epic: str,
        direction: str,
        size: float,
        entry: float,
        pnl_gbp: float | None,
        soft_loss_gbp: float | None = None,
        trail_floor_gbp: float | None = None,
        target_gbp: float | None = None,
        peak_profit_gbp: float | None = None,
        atr: float = 0.0,
        take_profit_level: float | None = None,
        source: str = "",
        updated_at: float | None = None,
    ) -> None:
        self.deal_id = str(deal_id or "")
        self.epic = str(epic or "")
        self.direction = str(direction or "BUY").upper()
        self.size = float(size or 0.0)
        self.entry = float(entry or 0.0)
        self.pnl_gbp = None if pnl_gbp is None else float(pnl_gbp)
        self.soft_loss_gbp = (
            None if soft_loss_gbp is None else float(soft_loss_gbp)
        )
        self.trail_floor_gbp = (
            None if trail_floor_gbp is None else float(trail_floor_gbp)
        )
        self.target_gbp = None if target_gbp is None else float(target_gbp)
        self.peak_profit_gbp = (
            None if peak_profit_gbp is None else float(peak_profit_gbp)
        )
        self.atr = float(atr or 0.0)
        self.take_profit_level = take_profit_level
        self.source = str(source or "")
        self.updated_at = float(updated_at if updated_at is not None else time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "epic": self.epic,
            "direction": self.direction,
            "size": self.size,
            "entry": self.entry,
            "pnl_gbp": self.pnl_gbp,
            "soft_loss_gbp": self.soft_loss_gbp,
            "trail_floor_gbp": self.trail_floor_gbp,
            "target_gbp": self.target_gbp,
            "peak_profit_gbp": self.peak_profit_gbp,
            "atr": self.atr,
            "take_profit_level": self.take_profit_level,
            "source": self.source,
            "updated_at": self.updated_at,
        }


def is_hollow_ghost_row(row: dict[str, Any]) -> bool:
    """True when a row must be hard-vetoed from the verified open array.

    Ghost exploit pattern: trade_support_overlay stubs with entry==0 and/or
    null pnl. Verified broker/gbp-track rows with a real entry level are kept
    even when UPL is momentarily null (valued on next tick).
    """
    if not isinstance(row, dict):
        return True
    # Keep critical alarms visible even if hollow — operator must see them
    if row.get("critical_alarm") or row.get("flatten_failed"):
        return False
    try:
        entry = float(row.get("entry") or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    if entry <= 0.0:
        return True
    source = str(row.get("source") or "")
    pnl = row.get("pnl_gbp")
    # Overlay-synthesized ghosts often have null pnl — veto those specifically.
    if pnl is None and source in ("trade_support_overlay", ""):
        return True
    return False


def _tp_level(entry: float, direction: str, atr: float) -> float | None:
    if not (entry > 0 and atr > 0):
        return None
    buy = str(direction).upper() != "SELL"
    delta = ATR_TP_MULT * atr
    return entry + delta if buy else entry - delta


class MemoryContext:
    """Process-singleton in-RAM matrix. No disk, no file locks."""

    __slots__ = (
        "_lock",
        "_opens",
        "_dropped_hollow",
        "_quote_age_sec",
        "_quotes_fresh",
        "_quote_budget_sec",
        "_updated_at",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._opens: dict[str, OpenPositionMem] = {}
        self._dropped_hollow = 0
        self._quote_age_sec: float | None = None
        self._quotes_fresh = False
        self._quote_budget_sec: float = float(QUOTE_FRESHNESS_SEC)
        self._updated_at = 0.0

    def sync_open_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        atr_by_epic: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Replace verified opens from IG/REST snapshot rows; drop hollow ghosts.

        Returns the filtered row list (same dict objects, hollow removed).
        """
        atr_by_epic = atr_by_epic or {}
        verified: list[dict[str, Any]] = []
        dropped = 0
        new_opens: dict[str, OpenPositionMem] = {}
        now = time.time()
        for row in rows or []:
            if not isinstance(row, dict):
                dropped += 1
                continue
            if is_hollow_ghost_row(row):
                dropped += 1
                continue
            deal_id = str(row.get("deal_id") or "").strip()
            if not deal_id:
                dropped += 1
                continue
            epic = str(row.get("epic") or "")
            atr = float(atr_by_epic.get(epic) or row.get("atr") or 0.0)
            entry = float(row.get("entry") or 0.0)
            direction = str(row.get("direction") or "BUY")
            ps = row.get("protection_summary") if isinstance(row.get("protection_summary"), dict) else {}
            soft = ps.get("soft_loss_gbp", row.get("soft_loss_gbp"))
            trail = ps.get("trail_floor_gbp", row.get("trail_floor_gbp"))
            target = ps.get("target_gbp", row.get("target_gbp"))
            peak = ps.get("peak_profit_gbp", row.get("peak_profit_gbp"))
            mem = OpenPositionMem(
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=float(row.get("size") or 0.0),
                entry=entry,
                pnl_gbp=row.get("pnl_gbp"),
                soft_loss_gbp=soft,
                trail_floor_gbp=trail,
                target_gbp=target,
                peak_profit_gbp=peak,
                atr=atr,
                take_profit_level=_tp_level(entry, direction, atr),
                source=str(row.get("source") or ""),
                updated_at=now,
            )
            new_opens[deal_id] = mem
            # Attach TP level for downstream consumers without mutating contract
            row = dict(row)
            row["take_profit_level"] = mem.take_profit_level
            row["memory_verified"] = True
            verified.append(row)

        with self._lock:
            self._opens = new_opens
            self._dropped_hollow += dropped
            self._updated_at = now
        try:
            from kernel.shm_facade import publish_position_risk

            for mem in new_opens.values():
                atr_pts = float(mem.atr or 0.0)
                publish_position_risk(
                    deal_id=mem.deal_id,
                    epic=mem.epic,
                    soft_loss_gbp=float(mem.soft_loss_gbp or 0.0),
                    trail_floor_gbp=float(mem.trail_floor_gbp or 0.0),
                    atr_limit_pts=atr_pts * ATR_TP_MULT,
                    atr_limit_gbp=float(mem.target_gbp or 0.0),
                    pnl_gbp=mem.pnl_gbp,
                    peak_profit_gbp=mem.peak_profit_gbp,
                )
        except Exception:
            pass
        return verified

    def set_quote_freshness(
        self, age_sec: float | None, *, budget_sec: float | None = None
    ) -> bool:
        """Update quote age; return True when within the transport-aware budget."""
        budget = _active_quote_budget_sec(budget_sec)
        fresh = age_sec is not None and float(age_sec) <= budget
        with self._lock:
            self._quote_age_sec = None if age_sec is None else float(age_sec)
            self._quotes_fresh = fresh
            self._quote_budget_sec = budget
        return fresh

    def quotes_fresh(self) -> bool:
        with self._lock:
            return bool(self._quotes_fresh)

    def quote_age_sec(self) -> float | None:
        with self._lock:
            return self._quote_age_sec

    def open_positions(self) -> list[OpenPositionMem]:
        with self._lock:
            return list(self._opens.values())

    def open_dicts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [m.to_dict() for m in self._opens.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._opens)

    def snapshot(self) -> dict[str, Any]:
        runtime = get_runtime_context().profiles_snapshot()
        with self._lock:
            return {
                "ok": True,
                "count": len(self._opens),
                "dropped_hollow_total": self._dropped_hollow,
                "quote_age_sec": self._quote_age_sec,
                "quotes_fresh": self._quotes_fresh,
                "quote_freshness_budget_sec": self._quote_budget_sec,
                "updated_at": self._updated_at,
                "positions": [m.to_dict() for m in self._opens.values()],
                "runtime": runtime,
                "active_asset": runtime.get("active_asset"),
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._opens.clear()
            self._dropped_hollow = 0
            self._quote_age_sec = None
            self._quotes_fresh = False
            self._quote_budget_sec = float(QUOTE_FRESHNESS_SEC)
            self._updated_at = 0.0
        reset_runtime_context_for_tests()


_CONTEXT: MemoryContext | None = None
_CTX_LOCK = threading.Lock()


def get_memory_context() -> MemoryContext:
    global _CONTEXT
    with _CTX_LOCK:
        if _CONTEXT is None:
            _CONTEXT = MemoryContext()
        return _CONTEXT


def reset_memory_context_for_tests() -> None:
    get_memory_context().reset_for_tests()
