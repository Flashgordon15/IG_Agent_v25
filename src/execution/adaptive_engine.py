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

    def settings(
        self,
        setup_key: str,
        adjusted_confidence: float,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = self._cfg
        atr_val, spread_val = self._snapshot_atr_spread(snapshot)
        risk = cfg.default_stop_distance_points
        reward = cfg.reward_multiple
        size = cfg.trade_size
        notes: list[str] = []

        if cfg.adaptive_execution_enabled:
            if cfg.adaptive_atr_risk_enabled and atr_val > 0:
                risk = self._clamp(
                    atr_val * cfg.atr_multiplier,
                    cfg.adaptive_min_risk_points,
                    cfg.adaptive_max_risk_points,
                )
                notes.append(f"ATR risk {risk:.1f}")

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
        return {
            "risk": risk,
            "reward": reward,
            "limit": limit,
            "size": size,
            "atr": atr_val,
            "spread": spread_val,
            "notes": ", ".join(notes) if notes else "standard execution",
        }

    def should_block(
        self,
        setup_key: str,
        adjusted_confidence: float,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        cfg = self._cfg
        if not cfg.adaptive_execution_enabled:
            return False, ""

        if adjusted_confidence < cfg.adaptive_min_adjusted_confidence:
            return True, "adaptive block: confidence below adaptive minimum"

        _, spread_val = self._snapshot_atr_spread(snapshot)
        if spread_val > cfg.adaptive_max_entry_spread:
            return True, f"adaptive block: spread {spread_val:.1f} > {cfg.adaptive_max_entry_spread:.1f}"

        if cfg.adaptive_block_bad_setups and self._memory:
            st = self._memory.setup_stats(setup_key)
            trades = int(st.get("trades") or 0) if st else 0
            if trades >= cfg.adaptive_min_setup_trades:
                wr = float(st.get("winrate") or 0)
                avg = float(st.get("avg_pnl") or 0)
                if wr <= cfg.adaptive_bad_winrate_threshold or avg < 0:
                    return (
                        True,
                        f"adaptive block: bad setup wr {wr:.0%} avg {avg:.1f} pts "
                        f"({trades} trades, need wr > {cfg.adaptive_bad_winrate_threshold:.0%})",
                    )

        return False, ""
