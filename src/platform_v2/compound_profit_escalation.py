"""
Compound Profit Escalation Matrix — Platform V2.

Reads post-milestone session P&L from trading_ledger.json and scales contract
size at £200 profit increments. Defensive reset to floor on >1.5% session drawdown.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_v2 import platform_v2_settings

try:
    from trading.micro_lot_verification import (
        micro_contract_size,
        micro_lot_verification_enabled,
    )
except ImportError:
    def micro_lot_verification_enabled() -> bool:  # type: ignore[misc]
        return False

    def micro_contract_size() -> float:  # type: ignore[misc]
        return 0.1

_DEFAULT_TIERS = (1.0, 1.5, 2.5, 4.0)
_PROFIT_STEP_GBP = 200.0
_DRAWDOWN_RESET_PCT = 0.015
_FLOOR_SIZE = 1.0

_lock = threading.Lock()
_session_peak_equity: float | None = None
_escalation_tripped: bool = False


@dataclass(frozen=True)
class EscalationResult:
    size: float
    tier_multiplier: float
    net_profit_gbp: float
    profit_step: int
    defensive_reset: bool
    reason: str


def _settings() -> dict[str, Any]:
    base = platform_v2_settings()
    block = base.get("compound_escalation")
    return dict(block) if isinstance(block, dict) else {}


def _ledger_path() -> Path:
    try:
        from target_reconciliation.live_fire_ledger import LEDGER_PATH

        return LEDGER_PATH
    except Exception:
        from system.paths import data_dir

        return data_dir() / "state" / "trading_ledger.json"


def _milestone_cutoff() -> datetime:
    try:
        from target_reconciliation.live_fire_ledger import MILESTONE_BASELINE_UTC

        return MILESTONE_BASELINE_UTC
    except Exception:
        return datetime(2026, 6, 23, 9, 0, tzinfo=timezone.utc)


def _filter_rows_since(rows: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = None
        for key in ("closed_at", "executed_at", "exit_timestamp", "timestamp"):
            raw = str(row.get(key) or "").strip()
            if not raw:
                continue
            try:
                text = raw.replace("Z", "+00:00")
                ts = datetime.fromisoformat(text)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if ts is None or ts >= since:
            out.append(row)
    return out


def read_session_net_profit_gbp(*, ledger_path: Path | None = None) -> float:
    """Net P&L from post-milestone closed rows in trading_ledger.json."""
    path = ledger_path or _ledger_path()
    if not path.is_file():
        return 0.0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0

    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and metrics.get("net_pnl_gbp") is not None:
        try:
            return float(metrics["net_pnl_gbp"])
        except (TypeError, ValueError):
            pass

    trades = payload.get("closed_trades") or payload.get("trades") or []
    if not isinstance(trades, list):
        return 0.0
    rows = _filter_rows_since([t for t in trades if isinstance(t, dict)], _milestone_cutoff())
    total = 0.0
    for row in rows:
        try:
            total += float(
                row.get("pnl_gbp")
                if row.get("pnl_gbp") is not None
                else row.get("net_pnl")
                or 0
            )
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def tier_multiplier_for_profit(net_profit_gbp: float) -> tuple[float, int]:
    cfg = _settings()
    step = float(cfg.get("profit_step_gbp", _PROFIT_STEP_GBP))
    raw_tiers = cfg.get("tiers") or list(_DEFAULT_TIERS)
    tiers = tuple(float(t) for t in raw_tiers)
    if not tiers:
        tiers = _DEFAULT_TIERS
    profit = max(0.0, float(net_profit_gbp))
    idx = int(profit // step) if step > 0 else 0
    idx = min(idx, len(tiers) - 1)
    return tiers[idx], idx


def _session_drawdown_pct(session_equity: float | None) -> tuple[float, bool]:
    """Trailing drawdown from session peak equity."""
    global _session_peak_equity, _escalation_tripped
    if session_equity is None or session_equity <= 0:
        return 0.0, False

    reset_pct = float(_settings().get("drawdown_reset_pct", _DRAWDOWN_RESET_PCT))
    with _lock:
        if _session_peak_equity is None or session_equity > _session_peak_equity:
            _session_peak_equity = session_equity
        peak = float(_session_peak_equity)

    if peak <= 0:
        return 0.0, False
    dd = (peak - session_equity) / peak
    tripped = dd > reset_pct
    if tripped:
        with _lock:
            _escalation_tripped = True
    return dd, tripped


def apply_compound_escalation(
    base_size: float,
    *,
    session_equity_gbp: float | None = None,
    ledger_path: Path | None = None,
) -> EscalationResult:
    """
    Scale *base_size* by profit tier; reset to floor on defensive drawdown spike.
    """
    cfg = _settings()
    if micro_lot_verification_enabled():
        micro = micro_contract_size()
        return EscalationResult(
            size=micro,
            tier_multiplier=1.0,
            net_profit_gbp=read_session_net_profit_gbp(ledger_path=ledger_path),
            profit_step=0,
            defensive_reset=False,
            reason=f"micro_lot_verification floor={micro}",
        )

    floor = float(cfg.get("floor_size", _FLOOR_SIZE))
    net_profit = read_session_net_profit_gbp(ledger_path=ledger_path)
    mult, step = tier_multiplier_for_profit(net_profit)

    dd_pct, dd_tripped = _session_drawdown_pct(session_equity_gbp)
    defensive = dd_tripped
    reason = f"tier={mult}x step={step} net=£{net_profit:.2f}"

    if defensive:
        mult = 1.0
        reason = f"defensive_reset dd={dd_pct * 100:.2f}% — {reason}"

    scaled = max(floor * 0.1, float(base_size) * mult)
    max_ceiling = float(cfg.get("max_size", 4.0))
    scaled = min(scaled, max_ceiling)

    return EscalationResult(
        size=round(scaled, 4),
        tier_multiplier=mult,
        net_profit_gbp=net_profit,
        profit_step=step,
        defensive_reset=defensive,
        reason=reason,
    )


def v2_max_order_size() -> float:
    """Transmission ceiling when platform V2 escalation is active."""
    if micro_lot_verification_enabled():
        return micro_contract_size()
    cfg = _settings()
    return float(cfg.get("max_size", 4.0))


def reset_compound_escalation_for_tests() -> None:
    global _session_peak_equity, _escalation_tripped
    with _lock:
        _session_peak_equity = None
        _escalation_tripped = False
