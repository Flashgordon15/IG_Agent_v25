"""
Adaptive execution — ATR stops, confidence/setup multipliers, size scaling.
All thresholds from :class:`system.config.Config`.
"""

from __future__ import annotations

from typing import Any

from system.config import Config
from system.config_loader import get_config


class AdaptiveEngine:
    def __init__(self, config: Config, memory_store: Any | None = None) -> None:
        self._cfg = config
        self._memory = memory_store

    @property
    def config(self) -> Config:
        return get_config()

    def attach_memory(self, memory_store: Any) -> None:
        self._memory = memory_store

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _snapshot_atr_spread(self, snapshot: dict[str, Any] | None) -> tuple[float, float]:
        atr_val, spread_val = 0.0, 0.0
        if not snapshot or snapshot.get("last") is None:
            return atr_val, spread_val
        last = snapshot["last"]
        try:
            atr_val = float(last.get("atr", 0) if hasattr(last, "get") else last["atr"])
        except (TypeError, ValueError, KeyError):
            pass
        try:
            spread_val = float(last.get("spread", 0) if hasattr(last, "get") else last["spread"])
        except (TypeError, ValueError, KeyError):
            pass
        return atr_val, spread_val

    # Session liquidity — how many ATRs of floor to use per session.
    # Lower = tighter stops allowed (liquid sessions); higher = wider floor (choppy/thin).
    _SESSION_FLOOR_ATR: dict[str, float] = {
        "london_morning": 2.5,
        "london_us_overlap": 2.0,
        "us_afternoon": 3.5,
        "late": 3.5,
        "asia_early": 4.0,
    }
    # Vol regime — additional ATR floor multiplier on top of session factor.
    _VOL_FLOOR_ATR: dict[str, float] = {
        "vollow": 1.5,
        "volnormal": 2.2,
        "volhigh": 2.8,
    }

    def _dynamic_stop_floor(
        self,
        atr_val: float,
        setup_key: str,
        adjusted_confidence: float,
        cfg: Config,
    ) -> float:
        """Compute a dynamic stop floor driven by session, vol regime and ML confidence.

        The floor is the ATR scaled by the worst of the session and vol-regime
        multipliers, then nudged tighter/wider by signal confidence.  This replaces
        the fixed adaptive_min_risk_points when dynamic_stop_floor_enabled is True.
        """
        if not cfg.dynamic_stop_floor_enabled:
            return float(cfg.adaptive_min_risk_points)

        parts = (setup_key or "").lower().split("|")
        session = parts[2] if len(parts) > 2 else ""
        vol_regime = parts[-1] if parts else "volnormal"

        session_mult = self._SESSION_FLOOR_ATR.get(session, 3.0)
        vol_mult = self._VOL_FLOOR_ATR.get(vol_regime, 2.2)
        # Worst-case (widest floor) of the two factors protects against thin + choppy.
        floor_mult = max(session_mult, vol_mult)

        # ML/confidence adjustment: sharper signal → fractionally tighter floor.
        if adjusted_confidence >= 95:
            floor_mult *= 0.85
        elif adjusted_confidence < 82:
            floor_mult *= 1.15

        dynamic_floor = atr_val * floor_mult

        abs_min = float(cfg.dynamic_stop_floor_min)
        abs_max = float(cfg.adaptive_max_risk_points)
        return max(abs_min, min(dynamic_floor, abs_max))

    def settings(
        self,
        setup_key: str,
        adjusted_confidence: float,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = get_config()
        atr_val, spread_val = self._snapshot_atr_spread(snapshot)
        risk = cfg.default_stop_distance_points
        reward = cfg.reward_multiple
        size = cfg.trade_size
        notes: list[str] = []

        if cfg.adaptive_execution_enabled:
            if cfg.adaptive_atr_risk_enabled and atr_val > 0:
                stop_floor = self._dynamic_stop_floor(
                    atr_val, setup_key, adjusted_confidence, cfg
                )
                risk = self._clamp(
                    atr_val * cfg.atr_multiplier,
                    stop_floor,
                    cfg.adaptive_max_risk_points,
                )
                floor_source = "dynamic" if cfg.dynamic_stop_floor_enabled else "fixed"
                notes.append(f"ATR risk {risk:.1f} (floor {stop_floor:.1f} {floor_source})")

            if adjusted_confidence >= cfg.adaptive_high_confidence:
                reward = max(reward, cfg.adaptive_high_confidence_reward_multiple)
                notes.append(f"high-confidence reward {reward:.2f}R")

            if self._memory:
                st = self._memory.setup_stats(setup_key)
                if st and int(st.get("trades") or 0) >= cfg.adaptive_min_setup_trades:
                    wr = float(st.get("winrate") or 0)
                    avg = float(st.get("avg_pnl") or 0)
                    if wr >= cfg.adaptive_good_winrate_threshold and avg > 0:
                        reward = max(reward, cfg.adaptive_good_setup_reward_multiple)
                        size *= cfg.adaptive_good_setup_multiplier
                        notes.append(f"good setup wr {wr:.0%}")
                    elif wr <= cfg.adaptive_bad_winrate_threshold or avg < 0:
                        reward = min(reward, cfg.adaptive_bad_setup_reward_multiple)
                        size *= cfg.adaptive_bad_setup_multiplier
                        notes.append(f"bad setup protected wr {wr:.0%}")

            size = self._clamp(size, cfg.adaptive_min_trade_size, cfg.adaptive_max_trade_size)

        if self._memory and getattr(self._memory, "circuit_breaker_half_size_active", lambda: False)():
            size = self._clamp(size * 0.5, cfg.adaptive_min_trade_size, cfg.adaptive_max_trade_size)
            notes.append("circuit breaker half-size resume")

        limit = risk * reward

        # Cap limit so it doesn't exceed a realistic multiple of ATR (market's daily range proxy).
        # Prevents unachievable targets when stop is very tight but reward_multiple is high.
        max_lim_mult = cfg.adaptive_max_limit_atr_multiple
        if max_lim_mult > 0 and atr_val > 0:
            atr_cap = atr_val * max_lim_mult
            if limit > atr_cap:
                limit = atr_cap
                notes.append(f"limit capped at {limit:.1f} ({max_lim_mult}×ATR)")

        daily_range_ratio = (spread_val / atr_val) if atr_val > 0 else 0.0
        return {
            "risk": risk,
            "reward": reward,
            "limit": limit,
            "size": size,
            "atr": atr_val,
            "spread": spread_val,
            "daily_range_ratio": daily_range_ratio,
            "notes": ", ".join(notes) if notes else "standard execution",
        }

    def should_block(
        self,
        setup_key: str,
        adjusted_confidence: float,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        cfg = get_config()
        if not cfg.adaptive_execution_enabled:
            return False, ""

        if adjusted_confidence < cfg.adaptive_min_adjusted_confidence:
            return True, "adaptive block: confidence below adaptive minimum"

        atr_val, spread_val = self._snapshot_atr_spread(snapshot)
        if spread_val > cfg.adaptive_max_entry_spread:
            return True, f"adaptive block: spread {spread_val:.1f} > {cfg.adaptive_max_entry_spread:.1f}"

        # Block if net profit after spread is below minimum (spread eats too much of the target).
        min_net = cfg.adaptive_min_net_profit_pts
        if min_net > 0 and spread_val > 0 and atr_val > 0:
            max_lim = atr_val * max(cfg.adaptive_max_limit_atr_multiple, cfg.reward_multiple)
            net_profit = max_lim - spread_val
            if net_profit < min_net:
                return (
                    True,
                    f"adaptive block: net profit {net_profit:.1f}pts (limit {max_lim:.1f} - spread {spread_val:.1f}) < min {min_net:.1f}pts",
                )

        if cfg.adaptive_block_bad_setups and self._memory:
            st = self._memory.setup_stats(setup_key)
            trades = int(st.get("trades") or 0) if st else 0
            if trades >= cfg.adaptive_min_setup_trades:
                wins = int(st.get("wins") or 0) if st else 0
                losses = int(st.get("losses") or 0) if st else 0
                decisive = wins + losses
                # Pure breakeven setups (no real P&L) carry no signal-quality information
                # and must not block future trades.
                if decisive == 0:
                    pass
                else:
                    wr = wins / decisive
                    avg = float(st.get("avg_pnl") or 0)
                    if wr <= cfg.adaptive_bad_winrate_threshold or avg < 0:
                        return (
                            True,
                            f"adaptive block: bad setup wr {wr:.0%} avg {avg:.1f} pts "
                            f"({decisive} decisive trades, need wr > {cfg.adaptive_bad_winrate_threshold:.0%})",
                        )

        return False, ""
