"""Capital envelope allocator — v26 Phase 3 stub (shadow / planning only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_capital_envelope() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("capital_envelope") or {}
    except (json.JSONDecodeError, OSError):
        return {}


@dataclass
class PortfolioAllocator:
    """Track concurrent and daily risk deployment against v26 envelope."""

    account_balance_gbp: float = 10_000.0
    max_concurrent_risk_gbp: float = 1_200.0
    max_daily_risk_deployed_gbp: float = 2_500.0
    max_daily_loss_gbp: float = 500.0
    min_available_gbp: float = 100.0
    reserve_pct: float = 0.10
    concurrent_risk_gbp: float = 0.0
    daily_deployed_gbp: float = 0.0
    daily_pnl_gbp: float = 0.0
    open_positions: int = 0

    @classmethod
    def from_config(cls, envelope: dict[str, Any] | None = None) -> PortfolioAllocator:
        env = envelope if envelope is not None else load_capital_envelope()
        return cls(
            account_balance_gbp=float(env.get("account_balance_gbp") or 10_000),
            max_concurrent_risk_gbp=float(env.get("max_concurrent_risk_gbp") or 1_200),
            max_daily_risk_deployed_gbp=float(
                env.get("max_daily_risk_deployed_gbp") or 2_500
            ),
            max_daily_loss_gbp=float(env.get("max_daily_loss_gbp") or 500),
            min_available_gbp=float(env.get("min_available_gbp") or 100),
            reserve_pct=float(env.get("reserve_pct") or 0.10),
        )

    @property
    def reserved_gbp(self) -> float:
        return self.account_balance_gbp * self.reserve_pct

    @property
    def available_gbp(self) -> float:
        return max(
            0.0,
            self.account_balance_gbp - self.reserved_gbp - self.concurrent_risk_gbp,
        )

    def can_allocate(self, risk_gbp: float) -> tuple[bool, str]:
        risk = float(risk_gbp)
        if risk <= 0:
            return False, "risk must be positive"
        if self.daily_pnl_gbp <= -self.max_daily_loss_gbp:
            return False, "daily loss limit reached"
        if self.concurrent_risk_gbp + risk > self.max_concurrent_risk_gbp:
            return (
                False,
                f"concurrent cap £{self.max_concurrent_risk_gbp:.0f} "
                f"(used £{self.concurrent_risk_gbp:.0f})",
            )
        if self.daily_deployed_gbp + risk > self.max_daily_risk_deployed_gbp:
            return (
                False,
                f"daily deploy cap £{self.max_daily_risk_deployed_gbp:.0f}",
            )
        if self.available_gbp < self.min_available_gbp:
            return False, f"available below min £{self.min_available_gbp:.0f}"
        return True, "ok"

    def record_intent(self, risk_gbp: float) -> None:
        """Shadow/planning only — track hypothetical deployment."""
        risk = float(risk_gbp)
        self.concurrent_risk_gbp += risk
        self.daily_deployed_gbp += risk
        self.open_positions += 1

    def release_risk(self, risk_gbp: float, *, pnl_gbp: float = 0.0) -> None:
        risk = float(risk_gbp)
        self.concurrent_risk_gbp = max(0.0, self.concurrent_risk_gbp - risk)
        self.daily_pnl_gbp += float(pnl_gbp)
        self.open_positions = max(0, self.open_positions - 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            "account_balance_gbp": self.account_balance_gbp,
            "concurrent_risk_gbp": round(self.concurrent_risk_gbp, 2),
            "daily_deployed_gbp": round(self.daily_deployed_gbp, 2),
            "daily_pnl_gbp": round(self.daily_pnl_gbp, 2),
            "open_positions": self.open_positions,
            "available_gbp": round(self.available_gbp, 2),
            "max_concurrent_risk_gbp": self.max_concurrent_risk_gbp,
            "max_daily_risk_deployed_gbp": self.max_daily_risk_deployed_gbp,
            "utilization_pct": round(
                100.0 * self.concurrent_risk_gbp / self.max_concurrent_risk_gbp,
                1,
            )
            if self.max_concurrent_risk_gbp
            else 0.0,
        }
