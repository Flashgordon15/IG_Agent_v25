"""
Unified configuration object — single source of truth for all runtime parameters.

All values are loaded from JSON via :mod:`system.config_loader`.
Modules must use ``Config`` properties only; no inline numeric defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Config:
    """Typed view over merged v24 + v22 adaptive autotrader settings."""

    _data: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def with_updates(self, **kwargs: Any) -> Config:
        data = dict(self._data)
        data.update(kwargs)
        return Config(_data=data)

    # --- Operating mode ---
    @property
    def operating_mode(self) -> str:
        return str(self._data["operating_mode"]).upper()

    @property
    def account_type(self) -> str:
        return str(self._data["account_type"]).upper()

    @property
    def auto_trade_enabled(self) -> bool:
        return bool(self._data["auto_trade_enabled"])

    @property
    def allow_live_trading(self) -> bool:
        return bool(self._data.get("allow_live_trading", False))

    @property
    def dry_run(self) -> bool:
        return bool(self._data.get("dry_run", True))

    # --- Market ---
    @property
    def epic(self) -> str:
        return str(self._data["epic"])

    @property
    def market_search(self) -> str:
        return str(self._data.get("market_search", ""))

    @property
    def currency_code(self) -> str:
        return str(self._data.get("currency_code", "GBP"))

    # --- Signal / indicators ---
    @property
    def signal_threshold(self) -> float:
        return float(self._data["signal_threshold"])

    @property
    def confidence_floor(self) -> float:
        """Minimum blended confidence for CAUTION/HEALTHY entry. Configurable to enable bootstrap."""
        return float(self._data.get("confidence_floor", 80.0))

    @property
    def confidence_floor_recovery_per_win(self) -> float:
        """How much the floor increases per win toward 80.0 during bootstrap."""
        return float(self._data.get("confidence_floor_recovery_per_win", 1.0))

    @property
    def fast_ema(self) -> int:
        return int(self._data["fast_ema"])

    @property
    def slow_ema(self) -> int:
        return int(self._data["slow_ema"])

    @property
    def rsi_period(self) -> int:
        return int(self._data["rsi_period"])

    @property
    def rsi_buy_min(self) -> float:
        return float(self._data["rsi_buy_min"])

    @property
    def rsi_buy_max(self) -> float:
        return float(self._data.get("rsi_buy_max", 0))

    @property
    def rsi_sell_max(self) -> float:
        return float(self._data["rsi_sell_max"])

    @property
    def rsi_sell_min(self) -> float:
        return float(self._data.get("rsi_sell_min", 0))

    @property
    def atr_period(self) -> int:
        return int(self._data["atr_period"])

    @property
    def min_atr_points(self) -> float:
        return float(self._data["min_atr_points"])

    @property
    def max_atr_points(self) -> float:
        return float(self._data.get("max_atr_points", 0))

    @property
    def vol_regime_filter_enabled(self) -> bool:
        return bool(self._data.get("vol_regime_filter_enabled", True))

    @property
    def max_consecutive_losses(self) -> int:
        return int(self._data.get("max_consecutive_losses", 3))

    @property
    def circuit_breaker_pause_minutes(self) -> int:
        return int(self._data.get("circuit_breaker_pause_minutes", 60))

    @property
    def momentum_gap_points(self) -> float:
        return float(self._data["momentum_gap_points"])

    @property
    def max_live_quotes(self) -> int:
        return int(self._data["max_live_quotes"])

    # --- Spread ---
    @property
    def max_spread(self) -> float:
        return float(self._data["max_spread"])

    @property
    def max_spread_points(self) -> float:
        return float(self._data.get("max_spread_points", self._data["max_spread"]))

    @property
    def adaptive_max_entry_spread(self) -> float:
        return float(self._data.get("adaptive_max_entry_spread", self.max_spread_points))

    @property
    def adaptive_max_limit_atr_multiple(self) -> float:
        """Cap limit at this multiple of ATR (0 = disabled). Prevents unreachable targets."""
        return float(self._data.get("adaptive_max_limit_atr_multiple", 0.0))

    @property
    def adaptive_min_net_profit_pts(self) -> float:
        """Min required (limit_distance - spread) before entry. 0 = disabled."""
        return float(self._data.get("adaptive_min_net_profit_pts", 0.0))

    # --- Adaptive execution ---
    @property
    def adaptive_execution_enabled(self) -> bool:
        return bool(self._data["adaptive_execution_enabled"])

    @property
    def adaptive_min_adjusted_confidence(self) -> float:
        return float(self._data["adaptive_min_adjusted_confidence"])

    @property
    def adaptive_atr_risk_enabled(self) -> bool:
        return bool(self._data["adaptive_atr_risk_enabled"])

    @property
    def adaptive_atr_risk_multiple(self) -> float:
        return float(self._data["adaptive_atr_risk_multiple"])

    @property
    def atr_multiplier(self) -> float:
        return self.adaptive_atr_risk_multiple

    @property
    def adaptive_min_risk_points(self) -> float:
        return float(self._data["adaptive_min_risk_points"])

    @property
    def adaptive_max_risk_points(self) -> float:
        return float(self._data["adaptive_max_risk_points"])

    @property
    def max_position_age_minutes(self) -> float | None:
        v = self._data.get("max_position_age_minutes")
        return float(v) if v is not None and float(v) > 0 else None

    @property
    def dynamic_stop_floor_enabled(self) -> bool:
        return bool(self._data.get("dynamic_stop_floor_enabled", False))

    @property
    def dynamic_stop_floor_min(self) -> float:
        return float(self._data.get("dynamic_stop_floor_min", 8.0))

    @property
    def adaptive_high_confidence(self) -> float:
        return float(self._data["adaptive_high_confidence"])

    @property
    def adaptive_high_confidence_reward_multiple(self) -> float:
        return float(self._data["adaptive_high_confidence_reward_multiple"])

    @property
    def adaptive_min_setup_trades(self) -> int:
        return int(self._data["adaptive_min_setup_trades"])

    @property
    def adaptive_good_winrate(self) -> float:
        return float(self._data["adaptive_good_winrate"])

    @property
    def adaptive_bad_winrate(self) -> float:
        return float(self._data["adaptive_bad_winrate"])

    @property
    def adaptive_good_winrate_threshold(self) -> float:
        return self.adaptive_good_winrate

    @property
    def adaptive_bad_winrate_threshold(self) -> float:
        return self.adaptive_bad_winrate

    @property
    def adaptive_good_setup_reward_multiple(self) -> float:
        return float(self._data["adaptive_good_setup_reward_multiple"])

    @property
    def adaptive_bad_setup_reward_multiple(self) -> float:
        return float(self._data["adaptive_bad_setup_reward_multiple"])

    @property
    def adaptive_good_setup_size_multiplier(self) -> float:
        return float(self._data["adaptive_good_setup_size_multiplier"])

    @property
    def adaptive_bad_setup_size_multiplier(self) -> float:
        return float(self._data["adaptive_bad_setup_size_multiplier"])

    @property
    def adaptive_good_setup_multiplier(self) -> float:
        return self.adaptive_good_setup_size_multiplier

    @property
    def adaptive_bad_setup_multiplier(self) -> float:
        return self.adaptive_bad_setup_size_multiplier

    @property
    def adaptive_block_bad_setups(self) -> bool:
        return bool(self._data.get("adaptive_block_bad_setups", False))

    @property
    def adaptive_min_trade_size(self) -> float:
        return float(self._data["adaptive_min_trade_size"])

    @property
    def adaptive_max_trade_size(self) -> float:
        return float(self._data["adaptive_max_trade_size"])

    @property
    def trade_size(self) -> float:
        return float(self._data["trade_size"])

    @property
    def risk_points(self) -> float:
        return float(self._data["risk_points"])

    @property
    def reward_multiple(self) -> float:
        return float(self._data["reward_multiple"])

    @property
    def default_stop_distance_points(self) -> float:
        return float(self._data.get("default_stop_distance_points", self.risk_points))

    @property
    def default_limit_distance_points(self) -> float:
        return float(
            self._data.get(
                "default_limit_distance_points",
                self.default_stop_distance_points * self.reward_multiple,
            )
        )

    @property
    def risk_per_trade(self) -> float:
        return float(self._data.get("risk_per_trade", self.risk_points))

    # --- Breakeven / trailing ---
    @property
    def breakeven_enabled(self) -> bool:
        return bool(self._data["breakeven_enabled"])

    @property
    def breakeven_trigger_points(self) -> float:
        return float(self._data["breakeven_trigger_points"])

    @property
    def breakeven_offset_points(self) -> float:
        return float(self._data["breakeven_offset_points"])

    @property
    def breakeven_lock_points(self) -> float:
        return float(self._data.get("breakeven_lock_points", self.breakeven_offset_points))

    @property
    def breakeven_once_per_position(self) -> bool:
        return bool(self._data.get("breakeven_once_per_position", True))

    @property
    def adaptive_trailing_stop_enabled(self) -> bool:
        return bool(self._data["adaptive_trailing_stop_enabled"])

    @property
    def adaptive_trailing_trigger_points(self) -> float:
        return float(self._data["adaptive_trailing_trigger_points"])

    @property
    def trailing_stop_trigger_points(self) -> float:
        return self.adaptive_trailing_trigger_points

    @property
    def adaptive_trailing_distance_points(self) -> float:
        return float(self._data["adaptive_trailing_distance_points"])

    @property
    def trailing_stop_step_points(self) -> float:
        return float(self._data.get("trailing_stop_step_points", self.adaptive_trailing_distance_points))

    @property
    def stop_distance_points(self) -> float:
        return float(self._data.get("stop_distance_points", self.default_stop_distance_points))

    @property
    def limit_distance_points(self) -> float:
        return float(self._data.get("limit_distance_points", self.default_limit_distance_points))

    # --- Risk manager ---
    @property
    def one_position_per_epic(self) -> bool:
        return bool(self._data.get("one_position_per_epic", True))

    @property
    def max_open_positions(self) -> int:
        if "max_open_positions" in self._data:
            return max(1, min(18, int(self._data["max_open_positions"])))
        return self.max_positions_per_epic

    @property
    def max_positions_per_epic(self) -> int:
        if "max_positions_per_epic" in self._data:
            return max(1, min(6, int(self._data["max_positions_per_epic"])))
        return 1 if self.one_position_per_epic else 3

    @property
    def cooldown_seconds(self) -> int:
        return int(self._data["cooldown_seconds"])

    @property
    def cooldown_minutes(self) -> float:
        return self.cooldown_seconds / 60.0

    @property
    def max_slippage_points(self) -> float:
        return float(self._data["max_slippage_points"])

    # --- Simulator ---
    @property
    def sim_latency_ms(self) -> float:
        return float(self._data["sim_latency_ms"])

    @property
    def simulated_latency_ms(self) -> float:
        return self.sim_latency_ms

    @property
    def sim_slippage_points(self) -> float:
        return float(self._data["sim_slippage_points"])

    @property
    def simulated_slippage(self) -> float:
        return self.sim_slippage_points

    @property
    def sim_fill_quality(self) -> float:
        return float(self._data["sim_fill_quality"])

    @property
    def simulated_fill_quality(self) -> float:
        return self.sim_fill_quality

    @property
    def sim_spread_multiplier(self) -> float:
        return float(self._data["sim_spread_multiplier"])

    @property
    def simulated_spread_multiplier(self) -> float:
        return self.sim_spread_multiplier

    # --- Learning ---
    @property
    def learning_enabled(self) -> bool:
        return bool(self._data["learning_enabled"])

    @property
    def learning_min_trades_per_setup(self) -> int:
        return int(self._data["learning_min_trades_per_setup"])

    @property
    def learning_max_bonus(self) -> float:
        return float(self._data["learning_max_bonus"])

    @property
    def learning_max_penalty(self) -> float:
        return float(self._data["learning_max_penalty"])

    # --- Paths ---
    @property
    def journal_file(self) -> str:
        return str(self._data["journal_file"])

    @property
    def learning_db(self) -> str:
        return str(self._data["learning_db"])

    @property
    def closed_trades_display_limit(self) -> int:
        return int(self._data.get("closed_trades_display_limit", 250))

    @property
    def closed_trades_display_hours(self) -> float:
        return float(self._data.get("closed_trades_display_hours", 168.0))

    @property
    def closed_trades_verify_hours(self) -> float:
        return float(self._data.get("closed_trades_verify_hours", 48.0))

    @property
    def transaction_history_days(self) -> int:
        return int(self._data.get("transaction_history_days", 7))

    @property
    def closed_trades_pnl_tolerance(self) -> float:
        return float(self._data.get("closed_trades_pnl_tolerance", 0.05))

    # --- Runtime ---
    @property
    def live_gate_min_arming_ticks(self) -> int:
        return int(self._data.get("live_gate_min_arming_ticks", 2))

    @property
    def live_gate_assessment_ticks(self) -> int:
        return int(self._data.get("live_gate_assessment_ticks", 6))

    @property
    def live_gate_stack_min_ticks(self) -> int:
        return max(1, int(self._data.get("live_gate_stack_min_ticks", 1)))

    @property
    def refresh_seconds(self) -> int:
        return int(self._data["refresh_seconds"])

    @property
    def test_replay_speed_multiplier(self) -> float:
        return float(self._data.get("test_replay_speed_multiplier", 1.0))

    @property
    def test_replay_tick_interval_seconds(self) -> float:
        return float(self._data.get("test_replay_tick_interval_seconds", 30.0))

    @property
    def test_replay_use_multi_regime(self) -> bool:
        return bool(self._data.get("test_replay_use_multi_regime", False))

    @property
    def session_refresh_minutes(self) -> int:
        return int(self._data.get("session_refresh_minutes", 10))

    @property
    def stream_poll_seconds(self) -> float:
        return float(self._data.get("stream_poll_seconds", 8.0))

    @property
    def streaming_transport(self) -> str:
        return str(self._data.get("streaming_transport", "auto")).lower().strip()

    @property
    def position_sync_seconds(self) -> float:
        return float(self._data.get("position_sync_seconds", 25.0))

    @property
    def position_sync_open_fast_seconds(self) -> float:
        return float(self._data.get("position_sync_open_fast_seconds", 15.0))

    @property
    def position_sync_open_relaxed_seconds(self) -> float:
        return float(self._data.get("position_sync_open_relaxed_seconds", 30.0))

    @property
    def position_sync_relaxed_below_confidence(self) -> float:
        return float(self._data.get("position_sync_relaxed_below_confidence", 70.0))

    @property
    def startup_countdown_seconds(self) -> float:
        raw = self._data.get("startup_countdown_seconds")
        if raw is None:
            raw = self._data.get("startup_delay_seconds", 15.0)
        return max(0.0, float(raw))

    @property
    def startup_delay_seconds(self) -> float:
        """Legacy alias for startup_countdown_seconds."""
        return self.startup_countdown_seconds

    @property
    def startup_countdown_seconds_soak(self) -> float:
        return max(0.0, float(self._data.get("startup_countdown_seconds_soak", 4.0)))

    @property
    def startup_countdown_seconds_warm_session(self) -> float:
        return max(0.0, float(self._data.get("startup_countdown_seconds_warm_session", 3.0)))

    @property
    def startup_countdown_warm_session_minutes(self) -> float:
        return max(1.0, float(self._data.get("startup_countdown_warm_session_minutes", 30.0)))

    @property
    def market_watch_enabled(self) -> bool:
        return bool(self._data.get("market_watch_enabled", True))

    @property
    def auto_flatten_on_session_end(self) -> bool:
        return bool(self._data.get("auto_flatten_on_session_end", False))

    @property
    def rest_budget_warn_per_minute(self) -> int:
        return max(1, int(self._data.get("rest_budget_warn_per_minute", 6)))

    @property
    def rest_hard_cap_per_minute(self) -> int:
        """Hard per-minute cap for non-essential REST calls — blocks unconditionally
        regardless of stream state. Defaults to warn_per_minute when not set."""
        default = self.rest_budget_warn_per_minute
        raw = self._data.get("rest_hard_cap_per_minute")
        if raw is None:
            return default
        return max(1, int(raw))

    @property
    def rest_min_interval_seconds(self) -> float:
        return float(self._data.get("rest_min_interval_seconds", 10.0))

    @property
    def transaction_sync_seconds(self) -> float:
        return float(self._data.get("transaction_sync_seconds", 60.0))

    @property
    def transaction_sync_min_gap_seconds(self) -> float:
        return float(self._data.get("transaction_sync_min_gap_seconds", 300.0))

    @property
    def max_retries(self) -> int:
        return int(self._data.get("max_retries", 4))

    @property
    def timeout_seconds(self) -> float:
        return float(self._data.get("timeout_seconds", 45))

    @property
    def retry_delay_seconds(self) -> float:
        return float(self._data.get("retry_delay_seconds", 2.5))

    # --- Risk limits (0 = disabled) ---
    @property
    def max_daily_loss_gbp(self) -> float:
        """Positive GBP daily loss limit from config (max_daily_loss_gbp key)."""
        v = float(self._data.get("max_daily_loss_gbp", 0) or 0)
        if v > 0:
            return v
        # Fall back to abs(max_daily_loss) if max_daily_loss_gbp not set
        fallback = abs(float(self._data.get("max_daily_loss", 0) or 0))
        return fallback if fallback > 0 else 200.0

    @property
    def max_daily_loss(self) -> float:
        return float(self._data.get("max_daily_loss", 0))

    @property
    def max_daily_trades(self) -> int:
        return int(self._data.get("max_daily_trades", 0))

    # --- Japan225 daily risk guardrails (0 = disabled, fallback to legacy keys) ---
    @property
    def max_daily_loss_amount(self) -> float:
        v = float(self._data.get("max_daily_loss_amount", 0) or 0)
        return v if v > 0 else self.max_daily_loss

    @property
    def max_trades_per_day(self) -> int:
        v = int(self._data.get("max_trades_per_day", 0) or 0)
        return v if v > 0 else self.max_daily_trades

    @property
    def min_account_balance(self) -> float:
        return float(self._data.get("min_account_balance", 0))

    @property
    def min_account_available(self) -> float:
        return float(self._data.get("min_account_available", 0))

    @property
    def max_open_risk_points(self) -> float:
        return float(self._data.get("max_open_risk_points", 0))

    # --- Session / trading hours ---
    @property
    def trading_hours_enabled(self) -> bool:
        return bool(self._data.get("trading_hours_enabled", False))

    @property
    def trading_session_whitelist(self) -> list[str]:
        default = ["asia_early", "london_morning", "london_us_overlap", "us_afternoon", "late"]
        return list(self._data.get("trading_session_whitelist", default))

    # --- Test parity ---
    @property
    def test_parity_mode(self) -> bool:
        return bool(self._data.get("test_parity_mode", False))

    @property
    def sim_apply_slippage(self) -> bool:
        return bool(self._data.get("sim_apply_slippage", False))

    # --- Telegram notifications ---
    @property
    def telegram(self) -> dict[str, Any]:
        raw = self._data.get("telegram")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram.get("enabled", False))

    @property
    def telegram_bot_token(self) -> str:
        return str(self.telegram.get("bot_token", "") or "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return str(self.telegram.get("chat_id", "") or "").strip()
