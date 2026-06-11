"""Final risk gate after adaptive sizing — config-driven."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from system.config import Config
from system.config_loader import get_config as _get_config


@dataclass
class RiskAssessment:
    approved: bool
    size: float
    stop_distance: float
    limit_distance: float
    reason: str = ""


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
        cfg = self._cfg
        size = float(execution_params.get("size", cfg.trade_size))
        stop = float(execution_params.get("risk", cfg.stop_distance_points))
        limit = float(execution_params.get("limit", stop * cfg.reward_multiple))
        gate_sourced = bool(execution_params.get("gate_sourced"))

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
                reason="Invalid size or stop distance",
            )
        if float(execution_params.get("spread", 0)) > cfg.max_spread_points:
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
            from system.daily_loss_policy import daily_loss_gate_status

            loss_ok, loss_detail, _meta = daily_loss_gate_status(self._store, cfg)
            if not loss_ok:
                return RiskAssessment(
                    approved=False,
                    size=size,
                    stop_distance=stop,
                    limit_distance=limit,
                    reason=loss_detail,
                )

        if self._store is not None and cfg.max_daily_trades > 0:
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

        return RiskAssessment(
            approved=True,
            size=size,
            stop_distance=stop,
            limit_distance=limit,
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
