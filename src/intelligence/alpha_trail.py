"""
Alpha-optimised trailing engine — volatility-adjusted ATR trail for scalping.

Targets aggressive profit lock on £1k/day cadence while giving winners room in
trending microstructure regimes. Pure math; delegates stop proposal to
trailing_stop_engine.eval_trailing_stop.

Institutional Capital Harvesting Contract ('A win is a win'):
  1. Anti-Regret BE at +15 pips → stop at entry + 1.5 pips
  2. 2R Liquidity Lock at +2R → stop at entry + 1R
  3. Parabolic Milestone Snap at >=75% daily target → lock 50% of milestone
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execution.trailing_stop_engine import TrailEval, eval_trailing_stop
from intelligence.types import AlphaTrailVerdict, MicroRegime
from system.engine_log import log_engine
from system.pnl_math import ig_points_to_price_delta, price_delta_to_ig_points

CAPITAL_PRESERVATION_ATR_MULT = 1.0
DEFAULT_BASE_ATR_MULT = 0.55
DEFAULT_TIGHTEN_ATR_MULT = 0.32
DEFAULT_RUN_ATR_MULT = 0.85
PROFIT_TIGHTEN_PTS = 12.0
SESSION_MILESTONE_PTS = 40.0

# --- Institutional Capital Harvesting Contract (immutable) ---
ANTI_REGRET_PROFIT_PIPS = 15.0
ANTI_REGRET_STOP_OFFSET_PIPS = 1.5
TWO_R_PROFIT_MULTIPLIER = 2.0
ONE_R_LOCK_MULTIPLIER = 1.0
PARABOLIC_MILESTONE_PCT = 0.75
PARABOLIC_LOCK_FLOOR_PCT = 0.50
PARABOLIC_POSITION_LOCK_RATIO = 0.50

_contract_log_state: dict[str, str] = {}


@dataclass(frozen=True)
class AlphaTrailPosition:
    epic: str
    side: str
    entry: float
    stop: float
    target: float
    atr_pts: float
    deal_id: str = ""


def _initial_risk_pts(side: str, entry: float, stop: float) -> float:
    side_u = str(side or "").upper()
    if side_u == "BUY":
        return max(0.0, float(entry) - float(stop))
    if side_u == "SELL":
        return max(0.0, float(stop) - float(entry))
    return 0.0


def _profit_pips(epic: str, profit_pts: float) -> float:
    return float(price_delta_to_ig_points(epic, profit_pts))


def _log_harvest_contract(
    *,
    deal_id: str,
    trigger: str,
    epic: str,
    side: str,
    profit_pips: float,
    proposed_stop: float,
    entry: float,
) -> None:
    key = str(deal_id or epic or "unknown")
    if _contract_log_state.get(key) == trigger:
        return
    _contract_log_state[key] = trigger
    log_engine(
        "CAPITAL_HARVEST "
        f"[{trigger}] deal={key} epic={epic} side={side} "
        f"profit_pips={profit_pips:.2f} stop={proposed_stop:.5f} entry={entry:.5f} "
        "— A win is a win (flight log)"
    )


def apply_capital_harvest_contract(
    *,
    epic: str,
    side: str,
    entry: float,
    stop: float,
    px: float,
    profit_pts: float,
    proposed_stop: float | None,
    deal_id: str = "",
    parabolic_snap_active: bool = False,
    p_day_gbp: float = 0.0,
    lock_floor_gbp: float = 500.0,
) -> tuple[float | None, str]:
    """
    Override trailing proposal with institutional harvest floors (near-zero I/O).

    Returns (proposed_stop, contract_detail).
    """
    side_u = str(side or "").upper()
    if side_u not in ("BUY", "SELL"):
        return proposed_stop, ""

    profit_pips = _profit_pips(epic, profit_pts)
    risk_pts = _initial_risk_pts(side_u, entry, stop)
    triggers: list[str] = []
    floors: list[float] = []

    # 1. Anti-Regret Break-Even — +15 pips → entry + 1.5 pips (commission covered)
    if profit_pips >= ANTI_REGRET_PROFIT_PIPS:
        offset = ig_points_to_price_delta(epic, ANTI_REGRET_STOP_OFFSET_PIPS)
        if side_u == "BUY":
            floors.append(float(entry) + offset)
        else:
            floors.append(float(entry) - offset)
        triggers.append("ANTI_REGRET_BE")

    # 2. 2R Liquidity Securing — +2R → lock +1R
    if risk_pts > 0 and profit_pts >= TWO_R_PROFIT_MULTIPLIER * risk_pts:
        lock_pts = ONE_R_LOCK_MULTIPLIER * risk_pts
        if side_u == "BUY":
            floors.append(float(entry) + lock_pts)
        else:
            floors.append(float(entry) - lock_pts)
        triggers.append("TWO_R_LOCK")

    # 3. Parabolic Milestone Snap — >=75% daily target → lock 50% of float / £500 floor
    if parabolic_snap_active and profit_pts > 0:
        lock_pts = profit_pts * PARABOLIC_POSITION_LOCK_RATIO
        if side_u == "BUY":
            floors.append(float(entry) + lock_pts)
        else:
            floors.append(float(entry) - lock_pts)
        triggers.append("PARABOLIC_SNAP")

    if not floors:
        return proposed_stop, ""

    if side_u == "BUY":
        contract_stop = max(floors)
        if contract_stop <= float(stop):
            contract_stop = max(contract_stop, float(stop))
        merged = contract_stop
        if proposed_stop is not None:
            merged = max(merged, float(proposed_stop))
        if merged <= float(stop) or merged >= px:
            return proposed_stop, ""
        primary = triggers[0]
        _log_harvest_contract(
            deal_id=deal_id,
            trigger=primary,
            epic=epic,
            side=side_u,
            profit_pips=profit_pips,
            proposed_stop=merged,
            entry=float(entry),
        )
        if parabolic_snap_active and p_day_gbp > 0:
            log_engine(
                "CAPITAL_HARVEST [PARABOLIC_SNAP] "
                f"P_day=£{p_day_gbp:.2f} >= 75% milestone — "
                f"£{lock_floor_gbp:.2f} cash equity floor engaged"
            )
        return merged, ",".join(triggers)

    contract_stop = min(floors)
    merged = contract_stop
    if proposed_stop is not None:
        merged = min(merged, float(proposed_stop))
    if merged >= float(stop) or merged <= px:
        return proposed_stop, ""
    primary = triggers[0]
    _log_harvest_contract(
        deal_id=deal_id,
        trigger=primary,
        epic=epic,
        side=side_u,
        profit_pips=profit_pips,
        proposed_stop=merged,
        entry=float(entry),
    )
    return merged, ",".join(triggers)


def reset_capital_harvest_contract_for_tests() -> None:
    _contract_log_state.clear()


class AlphaOptimisedTrailEngine:
    """
    Dynamic ATR trailing multiples tuned for high-frequency IG index scalping.

    - Low vol / momentum: wider trail (room to run)
    - High profit / session milestone: tighter trail (lock £1k/day cadence)
    - Sweep regimes: intermediate tighten after impulse
    """

    def __init__(
        self,
        *,
        base_atr_mult: float = DEFAULT_BASE_ATR_MULT,
        tighten_atr_mult: float = DEFAULT_TIGHTEN_ATR_MULT,
        run_atr_mult: float = DEFAULT_RUN_ATR_MULT,
        profit_tighten_pts: float = PROFIT_TIGHTEN_PTS,
        session_milestone_pts: float = SESSION_MILESTONE_PTS,
    ) -> None:
        self._base_mult = float(base_atr_mult)
        self._tight_mult = float(tighten_atr_mult)
        self._run_mult = float(run_atr_mult)
        self._profit_tighten = float(profit_tighten_pts)
        self._session_milestone = float(session_milestone_pts)

    def _atr_multiple(
        self,
        *,
        profit_pts: float,
        micro_regime: MicroRegime,
        session_profit_pts: float,
        risk_compression_factor: float = 1.0,
        capital_preservation: bool = False,
    ) -> tuple[float, bool, str]:
        if capital_preservation:
            return (
                CAPITAL_PRESERVATION_ATR_MULT,
                True,
                "capital preservation 1.0x ATR",
            )

        mult = self._base_mult
        tighten = False
        notes: list[str] = []

        if micro_regime in ("MOMENTUM_UP", "MOMENTUM_DOWN"):
            mult = self._run_mult
            notes.append("momentum room")
        elif micro_regime in ("SWEEP_BUY", "SWEEP_SELL"):
            mult = (self._base_mult + self._tight_mult) / 2.0
            notes.append("post-sweep tighten")

        if profit_pts >= self._profit_tighten:
            mult = min(mult, self._tight_mult)
            tighten = True
            notes.append(f"profit>={self._profit_tighten:.0f}pts")
        if session_profit_pts >= self._session_milestone:
            mult = self._tight_mult
            tighten = True
            notes.append("session milestone")

        factor = max(0.1, min(1.0, float(risk_compression_factor)))
        if factor < 1.0:
            floor = self._tight_mult
            mult = floor + (mult - floor) * factor
            tighten = True
            notes.append(f"target factor={factor:.2f}")

        return mult, tighten, ", ".join(notes) if notes else "base trail"

    @staticmethod
    def _profit_pts(side: str, entry: float, px: float) -> float:
        side_u = str(side or "").upper()
        if side_u == "BUY":
            return px - entry
        if side_u == "SELL":
            return entry - px
        return 0.0

    @staticmethod
    def _exit_px(side: str, bid: float, offer: float) -> float:
        return float(bid if str(side or "").upper() == "BUY" else offer)

    def compute(
        self,
        pos: AlphaTrailPosition,
        *,
        bid: float,
        offer: float,
        micro_regime: MicroRegime = "NEUTRAL",
        session_profit_pts: float = 0.0,
        trigger_atr_mult: float = 0.35,
        risk_compression_factor: float = 1.0,
        capital_preservation: bool = False,
        parabolic_snap_active: bool = False,
        p_day_gbp: float = 0.0,
        lock_floor_gbp: float = 500.0,
    ) -> AlphaTrailVerdict:
        atr = max(0.0, float(pos.atr_pts))
        if atr <= 0:
            return AlphaTrailVerdict(
                epic=pos.epic,
                side=pos.side,
                proposed_stop=None,
                trail_distance_pts=0.0,
                atr_multiple=0.0,
                profit_pts=0.0,
                tighten_mode=False,
                detail="atr_unavailable",
                deal_id=str(pos.deal_id or ""),
            )

        px = self._exit_px(pos.side, bid, offer)
        profit = self._profit_pts(pos.side, pos.entry, px)
        mult, tighten, note = self._atr_multiple(
            profit_pts=profit,
            micro_regime=micro_regime,
            session_profit_pts=session_profit_pts,
            risk_compression_factor=risk_compression_factor,
            capital_preservation=capital_preservation,
        )
        distance = atr * mult
        trigger = atr * float(trigger_atr_mult)

        ev = TrailEval(
            side=pos.side,
            entry=pos.entry,
            stop=pos.stop,
            target=pos.target,
            px=px,
            profit=profit,
            trigger=trigger,
            distance=distance,
        )
        proposed = eval_trailing_stop(ev)

        harvest_stop, harvest_detail = apply_capital_harvest_contract(
            epic=pos.epic,
            side=pos.side,
            entry=pos.entry,
            stop=pos.stop,
            px=px,
            profit_pts=profit,
            proposed_stop=proposed,
            deal_id=str(pos.deal_id or ""),
            parabolic_snap_active=parabolic_snap_active,
            p_day_gbp=p_day_gbp,
            lock_floor_gbp=lock_floor_gbp,
        )
        if harvest_stop is not None:
            proposed = harvest_stop
            if harvest_detail:
                note = f"{note}; harvest={harvest_detail}" if note else f"harvest={harvest_detail}"
                tighten = True

        return AlphaTrailVerdict(
            epic=pos.epic,
            side=pos.side,
            proposed_stop=proposed,
            trail_distance_pts=distance,
            atr_multiple=mult,
            profit_pts=profit,
            tighten_mode=tighten,
            detail=note,
            deal_id=str(pos.deal_id or ""),
        )

    def compute_for_position_map(
        self,
        position_map: dict[str, dict[str, Any]],
        *,
        epic_quotes: dict[str, dict[str, float]],
        micro_verdicts: dict[str, Any],
        risk_compression_factor: float = 1.0,
        capital_preservation: bool = False,
    ) -> dict[str, AlphaTrailVerdict]:
        """
        Independent trailing evaluation per ``dealId`` — stops never cross-contaminate.
        """
        try:
            from system.thread_affinity import pin_current_thread

            pin_current_thread(role="capital_harvest_trail")
        except Exception:
            pass

        parabolic_snap_active = False
        p_day_gbp = 0.0
        lock_floor_gbp = 500.0
        try:
            from intelligence.target_engine import get_target_engine, target_engine_enabled
            from system.config_loader import get_config

            if target_engine_enabled(get_config(reload=False)):
                te = get_target_engine()
                te.refresh()
                parabolic_snap_active = te.capital_harvest_milestone_snap_active()
                p_day_gbp = float(te.last_p_day)
                lock_floor_gbp = te.capital_harvest_lock_floor_gbp()
        except Exception:
            pass

        out: dict[str, AlphaTrailVerdict] = {}
        for deal_id, row in position_map.items():
            if not deal_id or not isinstance(row, dict):
                continue
            epic = str(row.get("epic") or "")
            side = str(row.get("side") or row.get("direction") or "")
            entry = float(row.get("entry") or row.get("level") or 0)
            stop = float(row.get("stop") or row.get("stop_level") or 0)
            atr = float(row.get("atr") or row.get("atr_pts") or 40.0)
            q = epic_quotes.get(epic) or {}
            bid = float(q.get("bid") or row.get("bid") or entry)
            offer = float(q.get("offer") or row.get("offer") or entry)
            regime = micro_verdicts.get(epic, {}).get("regime", "NEUTRAL")
            if regime not in (
                "NEUTRAL",
                "MOMENTUM_UP",
                "MOMENTUM_DOWN",
                "SWEEP_BUY",
                "SWEEP_SELL",
                "ORDER_BLOCK",
            ):
                regime = "NEUTRAL"
            verdict = self.compute(
                AlphaTrailPosition(
                    epic=epic,
                    side=side,
                    entry=entry,
                    stop=stop,
                    target=float(row.get("target") or entry),
                    atr_pts=atr,
                    deal_id=str(deal_id),
                ),
                bid=bid,
                offer=offer,
                micro_regime=regime,
                risk_compression_factor=risk_compression_factor,
                capital_preservation=capital_preservation,
                parabolic_snap_active=parabolic_snap_active,
                p_day_gbp=p_day_gbp,
                lock_floor_gbp=lock_floor_gbp,
            )
            out[str(deal_id)] = verdict
        return out

    def snapshot_config(self) -> dict[str, Any]:
        return {
            "base_atr_mult": self._base_mult,
            "tighten_atr_mult": self._tight_mult,
            "run_atr_mult": self._run_mult,
            "profit_tighten_pts": self._profit_tighten,
            "session_milestone_pts": self._session_milestone,
        }
