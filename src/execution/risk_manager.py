"""Final risk gate after adaptive sizing — config-driven."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from execution.volatility_risk_bracket import (
    BracketConfig as _BracketConfig,
    BracketQuote as _BracketQuote,
    BracketState as _BracketState,
    update_bracket as _update_bracket,
)
from system.config import Config

_DEFAULT_ATR_FALLBACK = 10.0


@dataclass
class RiskAssessment:
    approved: bool
    size: float
    stop_distance: float
    limit_distance: float
    reason: str = ""
    kelly_fraction: float = 0.0
    atr: float = 0.0
    contract_multiplier: float = 1.0
    margin_utilization_pct: float = 0.0


def resolve_atr_for_epic(epic: str) -> float:
    """ATR from regime engine or performance reviewer fallback."""
    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = evaluate_epic_regime(epic)
        atr_v = float(getattr(snap, "atr", 0) or 0)
        if atr_v > 0:
            return atr_v
    except Exception:
        pass
    try:
        from ai.strategy.performance_reviewer import active_14_bar_atr

        atr_v = active_14_bar_atr(epic)
        if atr_v and atr_v > 0:
            return float(atr_v)
    except Exception:
        pass
    return max(_DEFAULT_ATR_FALLBACK, 1.0)


def resolve_contract_multiplier(epic: str, cfg: Config) -> float:
    try:
        from trading.open_position_view import point_value_gbp_for_epic

        pv = float(point_value_gbp_for_epic(epic))
        if pv > 0:
            return pv
    except Exception:
        pass
    raw = cfg.as_dict() if hasattr(cfg, "as_dict") else {}
    return max(0.01, float(raw.get("ig_point_value_gbp") or 1.0))


HIGH_CONVICTION_ML_BAND = 0.70


def compute_continuous_kelly_fraction(
    *,
    base_kelly_cap: float,
    ml_expectation_score: float,
    veto_floor: float,
) -> float:
    """
    Continuous Kelly scaler — minimal capital near veto floor, full cap as ML → 1.0.

    Effective_Kelly = Base_Cap * ((ML - Veto_Floor) / (1.0 - Veto_Floor))
    """
    base = max(0.0, float(base_kelly_cap))
    ml = float(ml_expectation_score)
    floor = float(veto_floor)
    if base <= 0 or ml <= floor:
        return 0.0
    span = max(1e-6, 1.0 - floor)
    ratio = (ml - floor) / span
    ratio = max(0.0, min(1.0, ratio))
    if ml >= HIGH_CONVICTION_ML_BAND:
        boost = min(1.0, (ml - HIGH_CONVICTION_ML_BAND) / max(1e-6, 1.0 - HIGH_CONVICTION_ML_BAND))
        ratio = max(ratio, 0.65 + 0.35 * boost)
    return max(0.0, min(base, base * ratio))


def compute_kelly_position_size(
    *,
    equity: float,
    kelly_fraction: float,
    atr: float,
    contract_multiplier: float,
    ml_expectation_score: float | None = None,
    veto_floor: float | None = None,
) -> float:
    """Size = (Equity × Effective Kelly) / (ATR × Contract Multiplier)."""
    effective_kelly = float(kelly_fraction)
    if ml_expectation_score is not None and veto_floor is not None:
        effective_kelly = compute_continuous_kelly_fraction(
            base_kelly_cap=kelly_fraction,
            ml_expectation_score=ml_expectation_score,
            veto_floor=veto_floor,
        )
    denom = max(1e-9, float(atr) * max(0.01, float(contract_multiplier)))
    return max(0.01, (float(equity) * max(0.0, effective_kelly)) / denom)


class RiskManager:
    def __init__(self, config: Config, store: Any | None = None) -> None:
        self._cfg = config
        self._store = store

    @property
    def config(self) -> Config:
        return self._cfg

    def assess(
        self,
        *,
        direction: str,
        execution_params: dict[str, Any],
        account_balance: float | None = None,
        account_available: float | None = None,
    ) -> RiskAssessment:
        """Final pre-broker entry gate — does not apply to exits or stop dispatch."""
        cfg = self._cfg
        gate_sourced = bool(execution_params.get("gate_sourced"))
        epic = str(execution_params.get("epic") or execution_params.get("market_epic") or "")
        eq_meta: dict[str, Any] = {}
        equity = float(
            account_balance
            if account_balance is not None and account_balance > 0
            else (
                account_available
                if account_available is not None and account_available > 0
                else 10_000.0
            )
        )

        kelly_fraction = float(execution_params.get("kelly_fraction") or 0.0)
        route_path = str(execution_params.get("execution_path") or "")
        regime_state = int(execution_params.get("regime_state") or execution_params.get("regime") or -1)
        target_hold_sec = float(
            execution_params.get("target_hold_sec")
            or execution_params.get("hold_horizon_sec")
            or 8.0
        )
        flash_allocation = False
        if kelly_fraction <= 0:
            try:
                from runtime.master_orchestrator import get_strategy_route

                route = get_strategy_route(epic)
                if route:
                    kelly_fraction = float(route.get("kelly_fraction") or 0.0)
                    route_path = str(route.get("execution_path") or route_path)
            except Exception:
                pass
        if kelly_fraction <= 0:
            if route_path == "momentum_breakout":
                kelly_fraction = 0.25
            elif route_path == "limit_chase_hf":
                kelly_fraction = 0.15
            else:
                kelly_fraction = 0.10

        try:
            from runtime.portfolio_exploration_engine import evaluate_flash_allocation

            flash_allocation = evaluate_flash_allocation(
                execution_path=route_path,
                regime_state=regime_state,
                target_hold_sec=target_hold_sec,
            )
            if flash_allocation:
                kelly_fraction = max(kelly_fraction, 0.22)
        except Exception:
            flash_allocation = False

        atr = float(execution_params.get("atr") or 0.0)
        if atr <= 0 and epic:
            atr = resolve_atr_for_epic(epic)
        contract_mult = float(execution_params.get("contract_multiplier") or 0.0)
        if contract_mult <= 0 and epic:
            contract_mult = resolve_contract_multiplier(epic, cfg)

        ml_score = float(
            execution_params.get("win_probability")
            or execution_params.get("expectation_score")
            or execution_params.get("ml_expectation_score")
            or 0.0
        )
        veto_floor = 0.55
        try:
            from trading.probability_engine import resolve_dynamic_veto_floor

            veto_floor = float(resolve_dynamic_veto_floor(epic=epic))
        except Exception:
            pass

        effective_kelly = kelly_fraction
        if ml_score > 0:
            effective_kelly = compute_continuous_kelly_fraction(
                base_kelly_cap=kelly_fraction,
                ml_expectation_score=ml_score,
                veto_floor=veto_floor,
            )

        kelly_size = compute_kelly_position_size(
            equity=equity,
            kelly_fraction=kelly_fraction,
            atr=atr,
            contract_multiplier=contract_mult,
            ml_expectation_score=ml_score if ml_score > 0 else None,
            veto_floor=veto_floor if ml_score > 0 else None,
        )

        size = float(execution_params.get("size", cfg.trade_size))
        if kelly_size > 0:
            size = kelly_size if not gate_sourced else min(size, kelly_size * 1.25)

        try:
            from runtime.portfolio_exploration_engine import apply_covariance_compression

            size = apply_covariance_compression(size)
        except Exception:
            pass

        try:
            size, eq_meta = apply_equilibrium_risk_allocation(
                epic=epic,
                proposed_size=size,
                equity=equity,
                store=self._store,
            )
        except Exception:
            eq_meta = {}
        stop = float(execution_params.get("risk", cfg.stop_distance_points))
        limit = float(execution_params.get("limit", stop * cfg.reward_multiple))

        margin_util = 0.0
        try:
            from runtime.portfolio_exploration_engine import (
                HARD_MARGIN_LIMIT_GBP,
                _estimate_margin_used,
                _load_open_book,
            )

            used = _estimate_margin_used(_load_open_book())
            margin_util = used / max(HARD_MARGIN_LIMIT_GBP, 1.0)
        except Exception:
            pass

        if not gate_sourced:
            size = min(size, cfg.adaptive_max_trade_size)
            size = max(size, cfg.adaptive_min_trade_size)
            stop = min(stop, cfg.adaptive_max_risk_points)
            stop = max(stop, cfg.adaptive_min_risk_points)

        if size <= 0 or stop <= 0:
            return RiskAssessment(
                approved=False,
                size=size,
                stop_distance=stop,
                limit_distance=limit,
                reason=eq_meta.get("block_reason") or "Invalid size or stop distance",
                kelly_fraction=kelly_fraction,
                atr=atr,
                contract_multiplier=contract_mult,
                margin_utilization_pct=margin_util,
            )
        if float(execution_params.get("spread", 0)) > 0 and epic:
            spread_pts = float(execution_params.get("spread") or 0)
            ml_score = float(
                execution_params.get("win_probability")
                or execution_params.get("expectation_score")
                or 0.0
            )
            try:
                from runtime.portfolio_exploration_engine import vet_order_spread

                spread_ok, spread_reason, _ = vet_order_spread(
                    epic, spread_pts, expectation_score=ml_score
                )
                if not spread_ok:
                    return RiskAssessment(
                        approved=False,
                        size=size,
                        stop_distance=stop,
                        limit_distance=limit,
                        reason=spread_reason,
                    )
            except Exception:
                if spread_pts > cfg.max_spread_points:
                    return RiskAssessment(
                        approved=False,
                        size=size,
                        stop_distance=stop,
                        limit_distance=limit,
                        reason=f"Spread exceeds max {cfg.max_spread_points}",
                    )
        elif float(execution_params.get("spread", 0)) > cfg.max_spread_points:
            return RiskAssessment(
                approved=False,
                size=size,
                stop_distance=stop,
                limit_distance=limit,
                reason=f"Spread exceeds max {cfg.max_spread_points}",
            )

        if cfg.min_account_available > 0 and account_available is not None:
            if account_available < cfg.min_account_available:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=(
                        f"Available balance {account_available:.2f} "
                        f"below minimum {cfg.min_account_available:.2f}"
                    ),
                )

        if cfg.min_account_balance > 0 and account_balance is not None:
            if account_balance < cfg.min_account_balance:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=(
                        f"Account balance {account_balance:.2f} "
                        f"below minimum {cfg.min_account_balance:.2f}"
                    ),
                )

        if self._store is not None and cfg.max_daily_loss_gbp > 0:
            from system.daily_loss_policy import daily_loss_gate_status, effective_daily_loss_gbp

            fuse = get_equity_curve_trailing_fuse_snapshot()
            loss_ok, loss_detail, _meta = daily_loss_gate_status(self._store, cfg)
            if fuse.get("defensive_fuse_active"):
                loss = effective_daily_loss_gbp(self._store)
                eff_soft = float(fuse.get("effective_l1_loss_gbp") or 0.0)
                eff_hard = float(fuse.get("effective_l2_loss_gbp") or cfg.max_daily_loss_gbp)
                if loss >= eff_hard:
                    loss_ok = False
                    loss_detail = f"equilibrium fuse L2: loss £{loss:.2f} >= £{eff_hard:.0f}"
                elif loss >= eff_soft:
                    loss_ok = False
                    loss_detail = f"equilibrium fuse L1: loss £{loss:.2f} >= £{eff_soft:.0f}"
            if not loss_ok:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=loss_detail,
                )

        if self._store is not None:
            from trading.manual_intervention import entries_blocked_by_shield

            shield_blocked, shield_reason = entries_blocked_by_shield(self._store, cfg)
            if shield_blocked:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=shield_reason,
                )

        from trading.entry_protection import is_session_unlimited_trades

        if (
            self._store is not None
            and cfg.max_daily_trades > 0
            and not is_session_unlimited_trades()
        ):
            opened_today = int(self._store.count_trades_opened_today())
            if opened_today >= cfg.max_daily_trades:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=(
                        f"Daily trade limit reached ({opened_today}/"
                        f"{cfg.max_daily_trades})"
                    ),
                )

        max_exposure = cfg.max_open_risk_points
        if max_exposure > 0 and self._store is not None:
            open_risk = float(self._store.sum_open_risk_points())
            trade_risk = size * stop
            if open_risk + trade_risk > max_exposure:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=(
                        f"Open risk exposure {open_risk + trade_risk:.1f} "
                        f"exceeds max {max_exposure:.1f}"
                    ),
                )

        from execution.economic_check import check_risk_cap

        conf = float(execution_params.get("sizing_confidence") or 0)
        band = str(execution_params.get("risk_band") or "")
        cap_ok, risk_gbp, cap_gbp = check_risk_cap(
            size=size,
            stop_pts=stop,
            cfg=cfg,
            confidence=conf,
            risk_band_label=band,
        )
        if not cap_ok:
            return RiskAssessment(
                approved=False,
                size=size,
                stop_distance=stop,
                limit_distance=limit,
                reason=(
                    f"Risk £{risk_gbp:.2f} exceeds £{cap_gbp:.0f} cap "
                    f"(sovereign pre-broker check)"
                ),
            )

        epic = str(execution_params.get("epic") or execution_params.get("market_epic") or "")
        try:
            from system.volatility_risk_engine import apply_volatility_risk

            vr = apply_volatility_risk(
                epic=epic,
                size=size,
                stop_distance=stop,
                limit_distance=limit,
                store=self._store,
            )
            if not vr.approved:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=vr.reason or "volatility_risk_blocked",
                )
            size = vr.size
            stop = vr.stop_distance
            limit = vr.limit_distance
        except Exception:
            pass

        try:
            from runtime.portfolio_exploration_engine import assess_portfolio_exploration

            pe = assess_portfolio_exploration(
                epic=epic,
                direction=direction,
                size=size,
                stop_distance=stop,
                limit_distance=limit,
                account_available=account_available,
                account_balance=account_balance,
                execution_path=route_path,
                regime_state=regime_state,
                target_hold_sec=target_hold_sec,
                win_probability=float(execution_params.get("win_probability") or 0.0),
                flash_allocation=flash_allocation,
            )
            if not pe.approved:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=pe.reason or "portfolio_exploration_blocked",
                )
            size = pe.size
        except Exception:
            pass

        return RiskAssessment(
            approved=True,
            size=size,
            stop_distance=stop,
            limit_distance=limit,
            kelly_fraction=effective_kelly,
            atr=atr,
            contract_multiplier=contract_mult,
            margin_utilization_pct=margin_util,
        )

    def margin_preflight(
        self,
        *,
        account_available: float | None,
        open_count: int,
        max_positions: int,
    ) -> tuple[bool, str]:
        """Block gate before broker reject when stacking with low available margin."""
        if account_available is None:
            return True, ""
        cfg = self._cfg
        if cfg.min_account_available > 0 and account_available < cfg.min_account_available:
            return False, (
                f"Available balance {account_available:.2f} "
                f"below minimum {cfg.min_account_available:.2f}"
            )
        open_count = max(0, int(open_count))
        try:
            from runtime.portfolio_exploration_engine import get_dynamic_max_concurrent_trades

            max_positions = max(
                max_positions,
                get_dynamic_max_concurrent_trades(account_available=account_available),
            )
        except Exception:
            pass
        max_positions = max(1, int(max_positions))
        if open_count <= 0 or open_count >= max_positions:
            return True, ""
        leg_size = max(float(cfg.trade_size), float(cfg.adaptive_min_trade_size))
        stop_pts = max(float(cfg.stop_distance_points), 10.0)
        headroom = leg_size * stop_pts * 15.0
        if account_available < headroom:
            return False, (
                f"Low margin headroom ({account_available:.0f} available, "
                f"need ~{headroom:.0f} for next entry)"
            )
        return True, ""

    def max_risk_points(self) -> float:
        return self._cfg.adaptive_max_risk_points

    def max_trade_size(self) -> float:
        return self._cfg.adaptive_max_trade_size


# --- Asymmetric trailing stop matrix ---
_LONG_ATR_TRAIL_MULT = 2.5
_SHORT_EMA_ALPHA = 0.35
_SHORT_TICK_WINDOW = 5
_asymmetric_lock = threading.Lock()
_tick_highs: dict[str, deque[float]] = {}
_short_ema_high: dict[str, float] = {}
_asymmetric_last: dict[str, dict[str, Any]] = {}

_volatility_bracket_states: dict[str, _BracketState] = {}
_volatility_bracket_last: dict[str, dict[str, Any]] = {}


def record_asymmetric_tick(epic: str, *, bid: float, offer: float) -> None:
    """Feed 5-tick high stream for short-side aggressive trailing."""
    key = str(epic or "").strip()
    if not key:
        return
    touch = max(float(bid), float(offer))
    with _asymmetric_lock:
        hist = _tick_highs.setdefault(key, deque(maxlen=_SHORT_TICK_WINDOW))
        hist.append(touch)
        prev = float(_short_ema_high.get(key) or touch)
        alpha = _SHORT_EMA_ALPHA
        ema = alpha * touch + (1.0 - alpha) * prev
        _short_ema_high[key] = ema


def compute_asymmetric_trail_stop(
    *,
    epic: str,
    side: str,
    entry: float,
    current_stop: float,
    atr: float,
    bid: float,
    offer: float,
) -> dict[str, Any]:
    """
    Long: relaxed ATR trailing (2.5x). Short: aggressive 5-tick EMA-high tightening.
    """
    key = str(epic or "").strip()
    side_u = str(side or "").upper()
    px = float(bid if side_u == "BUY" else offer)
    atr_v = max(1e-9, float(atr))
    stop = float(current_stop)
    entry_v = float(entry)

    record_asymmetric_tick(key, bid=bid, offer=offer)

    if side_u == "BUY":
        trail_dist = atr_v * _LONG_ATR_TRAIL_MULT
        candidate = px - trail_dist
        new_stop = max(stop, candidate, entry_v * 0.0)
        mode = "long_relaxed_atr"
        mult = _LONG_ATR_TRAIL_MULT
    else:
        with _asymmetric_lock:
            ema_high = float(_short_ema_high.get(key) or px)
        candidate = min(px + atr_v * 0.35, ema_high)
        new_stop = min(stop if stop > 0 else candidate * 2, candidate)
        if new_stop <= px:
            new_stop = px + max(atr_v * 0.15, 1e-6)
        mode = "short_ema_high_tighten"
        mult = round(ema_high / max(px, 1e-9), 4)

    changed = abs(new_stop - stop) > 1e-9
    row = {
        "epic": key,
        "side": side_u,
        "mode": mode,
        "atr": round(atr_v, 4),
        "multiplier": mult,
        "previous_stop": round(stop, 6),
        "proposed_stop": round(new_stop, 6),
        "changed": changed,
        "ts": time.time(),
    }
    with _asymmetric_lock:
        _asymmetric_last[key] = row
    return row


def get_asymmetric_risk_snapshot() -> dict[str, Any]:
    with _asymmetric_lock:
        rows = [dict(v) for v in _asymmetric_last.values()]
    return {
        "ok": True,
        "long_atr_mult": _LONG_ATR_TRAIL_MULT,
        "short_ema_alpha": _SHORT_EMA_ALPHA,
        "positions": rows[-16:],
    }


def reset_asymmetric_risk_for_tests() -> None:
    with _asymmetric_lock:
        _tick_highs.clear()
        _short_ema_high.clear()
        _asymmetric_last.clear()


def compute_volatility_adjusted_trail_stop(
    *,
    epic: str,
    side: str,
    entry: float,
    current_stop: float,
    target: float,
    atr: float,
    baseline_atr: float,
    bid: float,
    offer: float,
) -> dict[str, Any]:
    """
    Dynamic ATR bracket — vol-ratio trail tightening + flash-move ratchet.

    Reuses a persistent BracketState per epic so that ATR smoothing and
    prev_px flash detection carry across ticks (zero allocation on hot path).
    """
    key = str(epic or "").strip()
    side_u = str(side or "").upper()
    atr_v = max(1e-9, float(atr))
    base_atr = max(1e-9, float(baseline_atr or atr_v))
    stop = float(current_stop)

    with _asymmetric_lock:
        state = _volatility_bracket_states.get(key)

    if state is None or state.side != side_u or state.entry != float(entry):
        side_l = "BUY" if side_u == "BUY" else "SELL"
        target_v = float(target)
        if target_v <= 0:
            target_v = (float(entry) + atr_v * 8.0) if side_u == "BUY" else (float(entry) - atr_v * 8.0)
        state = _BracketState(
            side=side_l,
            entry=float(entry),
            stop=stop,
            target=target_v,
            entry_atr=atr_v,
            baseline_atr=base_atr,
            live_atr=atr_v,
            prev_px=float(entry),
        )
    else:
        state.stop = stop

    upd = _update_bracket(state, _BracketQuote(bid=bid, offer=offer, live_atr=atr_v))
    row = {
        "epic": key,
        "side": side_u,
        "mode": upd.mode,
        "atr": round(atr_v, 6),
        "baseline_atr": round(base_atr, 6),
        "vol_ratio": round(upd.vol_ratio, 4),
        "trail_atr_mult": round(upd.trail_atr_mult, 4),
        "trail_distance": round(upd.trail_distance, 6),
        "previous_stop": round(stop, 6),
        "proposed_stop": round(upd.stop, 6),
        "stop_hit": upd.stop_hit,
        "changed": abs(upd.stop - stop) > 1e-9,
        "ts": time.time(),
    }
    with _asymmetric_lock:
        _volatility_bracket_states[key] = state
        _volatility_bracket_last[key] = row
    return row


def get_volatility_bracket_snapshot() -> dict[str, Any]:
    """Snapshot of all active volatility brackets for GUI broadcast."""
    with _asymmetric_lock:
        rows = [dict(v) for v in _volatility_bracket_last.values()]
    return {
        "ok": True,
        "positions": rows[-16:],
    }


def reset_volatility_bracket_for_tests() -> None:
    with _asymmetric_lock:
        _volatility_bracket_states.clear()
        _volatility_bracket_last.clear()


# --- Global Equilibrium Risk Allocator (£10k capital cap) ---
EQUILIBRIUM_EQUITY_CAP_GBP = 10_000.0
_PP_DEFENSE_FUSE_THRESHOLD = 800
_L1_BASE_DRAWDOWN_PCT = 2.0
_L2_BASE_DRAWDOWN_PCT = 4.0
_L1_DEFENSIVE_MULT = 0.75
_L2_DEFENSIVE_MULT = 0.75
_equilibrium_lock = threading.Lock()
_equilibrium_weights: dict[str, float] = {}
_equilibrium_last: dict[str, Any] = {}


def get_equity_curve_trailing_fuse_snapshot() -> dict[str, Any]:
    """Compress L1/L2 drawdown triggers when PlatformScoreboard drops below 800 PP."""
    pp = 1000
    tier = "standard"
    try:
        from runtime.master_orchestrator import (
            PP_DEFENSE_THRESHOLD,
            TELEMETRY_TIER_DEFENSE,
            get_platform_scoreboard,
        )

        sb = get_platform_scoreboard()
        pp = int(sb.total_pp)
        tier = sb.telemetry_tier_unlocked()
        defensive = pp <= PP_DEFENSE_THRESHOLD or tier == TELEMETRY_TIER_DEFENSE
    except Exception:
        defensive = False

    l1_mult = _L1_DEFENSIVE_MULT if defensive else 1.0
    l2_mult = _L2_DEFENSIVE_MULT if defensive else 1.0
    l1_pct = _L1_BASE_DRAWDOWN_PCT * l1_mult
    l2_pct = _L2_BASE_DRAWDOWN_PCT * l2_mult
    l1_loss = EQUILIBRIUM_EQUITY_CAP_GBP * l1_pct / 100.0
    l2_loss = EQUILIBRIUM_EQUITY_CAP_GBP * l2_pct / 100.0
    return {
        "ok": True,
        "platform_pp": pp,
        "defensive_fuse_active": defensive,
        "l1_drawdown_pct": round(l1_pct, 3),
        "l2_drawdown_pct": round(l2_pct, 3),
        "l1_delta_pct": round(_L1_BASE_DRAWDOWN_PCT - l1_pct, 3),
        "l2_delta_pct": round(_L2_BASE_DRAWDOWN_PCT - l2_pct, 3),
        "effective_l1_loss_gbp": round(l1_loss, 2),
        "effective_l2_loss_gbp": round(l2_loss, 2),
        "ts": time.time(),
    }


def apply_equilibrium_risk_allocation(
    *,
    epic: str,
    proposed_size: float,
    equity: float = EQUILIBRIUM_EQUITY_CAP_GBP,
    store: Any | None = None,
) -> tuple[float, dict[str, Any]]:
    """
    Normalize Kelly sizing so combined initial margin never breaches £10k equity cap.
    """
    key = str(epic or "").strip()
    size = max(0.0, float(proposed_size))
    meta: dict[str, Any] = {
        "epic": key,
        "proposed_size": round(size, 4),
        "equity_cap_gbp": EQUILIBRIUM_EQUITY_CAP_GBP,
    }
    if size <= 0:
        meta["block_reason"] = "zero_proposed_size"
        return 0.0, meta

    try:
        from runtime.portfolio_exploration_engine import (
            _estimate_margin_used,
            _load_open_book,
            regime_adjusted_margin_per_trade,
        )

        book = _load_open_book()
        used = _estimate_margin_used(book)
        per_unit_margin = max(1.0, regime_adjusted_margin_per_trade())
        proposed_margin = max(50.0, size * per_unit_margin)
        cap = min(float(equity), EQUILIBRIUM_EQUITY_CAP_GBP)
        headroom = max(0.0, cap - used)
        scale = 1.0
        if proposed_margin > headroom and proposed_margin > 0:
            scale = headroom / proposed_margin
        adjusted = size * scale
        if adjusted < 0.01:
            meta["block_reason"] = f"equilibrium_margin_ceiling used={used:.0f} headroom={headroom:.0f}"
            return 0.0, meta

        weights: dict[str, float] = {}
        total_margin = used + max(50.0, adjusted * per_unit_margin)
        for row in book:
            e = str(row.get("epic") or "")
            if not e:
                continue
            row_margin = max(50.0, float(row.get("size") or 0) * per_unit_margin)
            weights[e] = round(row_margin / max(total_margin, 1e-9), 4)
        if key:
            weights[key] = round(
                max(50.0, adjusted * per_unit_margin) / max(total_margin, 1e-9), 4
            )

        fuse = get_equity_curve_trailing_fuse_snapshot()
        if fuse.get("defensive_fuse_active"):
            adjusted *= 0.85

        meta.update(
            {
                "adjusted_size": round(adjusted, 4),
                "scale_factor": round(scale, 4),
                "margin_used_gbp": round(used, 2),
                "proposed_margin_gbp": round(proposed_margin, 2),
                "headroom_gbp": round(headroom, 2),
                "allocation_weight": weights.get(key, 0.0),
                "drawdown_fuse": fuse,
            }
        )
        with _equilibrium_lock:
            _equilibrium_weights.clear()
            _equilibrium_weights.update(weights)
            _equilibrium_last.clear()
            _equilibrium_last.update(meta)
        return adjusted, meta
    except Exception as exc:
        meta["block_reason"] = f"equilibrium_error:{type(exc).__name__}"
        return size, meta


def get_equilibrium_risk_snapshot() -> dict[str, Any]:
    with _equilibrium_lock:
        return {
            "ok": True,
            "equity_cap_gbp": EQUILIBRIUM_EQUITY_CAP_GBP,
            "weights": dict(_equilibrium_weights),
            "last_allocation": dict(_equilibrium_last),
            "drawdown_fuse": get_equity_curve_trailing_fuse_snapshot(),
        }


def reset_equilibrium_risk_for_tests() -> None:
    with _equilibrium_lock:
        _equilibrium_weights.clear()
        _equilibrium_last.clear()
