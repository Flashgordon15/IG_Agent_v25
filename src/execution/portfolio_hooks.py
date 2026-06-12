"""Wire v26 portfolio envelope to live entry/exit (non-blocking)."""

from __future__ import annotations

from datetime import date
from typing import Any

from execution.trade_risk import risk_gbp_from_row as risk_gbp_from_trade_row
from execution.types import TradeSignal
from system.engine_log import log_engine

_deal_risk_gbp: dict[str, float] = {}


def reset_portfolio_hooks_for_tests() -> None:
    _deal_risk_gbp.clear()
    try:
        from system.portfolio_envelope import reset_portfolio_envelope_for_tests

        reset_portfolio_envelope_for_tests()
    except Exception:
        pass


def _risk_gbp_from_params(
    execution_params: dict[str, Any],
    *,
    config: Any | None = None,
) -> float:
    stop = float(
        execution_params.get("risk")
        or execution_params.get("stop_distance")
        or execution_params.get("stop_pts")
        or 0
    )
    size = float(execution_params.get("size") or 0)
    point_value = 1.0
    if config is not None:
        try:
            point_value = float(config.get("ig_point_value_gbp", 1.0))
        except (TypeError, ValueError, AttributeError):
            point_value = 1.0
    if stop <= 0 or size <= 0:
        return 0.0
    return stop * size * point_value


def record_portfolio_entry_from_signal(
    deal_id: str,
    signal: TradeSignal,
    execution_params: dict[str, Any],
    *,
    config: Any | None = None,
) -> None:
    try:
        from system.portfolio_envelope import portfolio_gate_enabled, record_entry

        if not portfolio_gate_enabled():
            return
        risk = _risk_gbp_from_params(execution_params, config=config)
        if risk <= 0:
            return
        record_entry(risk)
        _deal_risk_gbp[str(deal_id)] = risk
        log_engine(
            f"portfolio_envelope entry deal={deal_id} risk_gbp={risk:.2f} "
            f"epic={signal.epic}"
        )
    except Exception as e:
        log_engine(
            f"portfolio_envelope entry hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )


def rehydrate_portfolio_from_store(
    store: Any, *, cfg: Any | None = None
) -> dict[str, float]:
    """Reload portfolio envelope + deal risk map from LearningStore open trades."""
    try:
        from system.portfolio_envelope import (
            portfolio_gate_enabled,
            rehydrate,
            snapshot,
        )
    except Exception:
        return {"concurrent": 0.0, "daily_deployed": 0.0, "daily_pnl": 0.0}

    if not portfolio_gate_enabled():
        return {"concurrent": 0.0, "daily_deployed": 0.0, "daily_pnl": 0.0}

    _deal_risk_gbp.clear()
    concurrent = 0.0
    today = date.today().isoformat()

    try:
        open_rows = store.active_trades()
    except Exception as e:
        log_engine(f"portfolio_envelope rehydrate: active_trades failed: {e}")
        open_rows = []

    for row in open_rows:
        if int(row["dry_run"] or 0):
            continue
        risk = risk_gbp_from_trade_row(row, cfg=cfg)
        if risk <= 0:
            continue
        concurrent += risk
        deal_id = str(row["ig_deal_id"] or row["deal_reference"] or row["id"] or "")
        if deal_id:
            _deal_risk_gbp[deal_id] = risk

    daily_deployed = concurrent
    try:
        cols = {
            r[1] for r in store.conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "opened_at" in cols:
            day_rows = store.conn.execute(
                """
                SELECT entry, stop, size, epic, dry_run
                FROM trades
                WHERE substr(opened_at, 1, 10) = ? AND dry_run = 0
                """,
                (today,),
            ).fetchall()
            daily_deployed = 0.0
            for row in day_rows:
                daily_deployed += risk_gbp_from_trade_row(row, cfg=cfg)
    except Exception:
        pass

    daily_pnl = 0.0
    try:
        from system.daily_loss_policy import effective_daily_pnl

        daily_pnl = float(effective_daily_pnl(store, day=today))
    except Exception:
        pass

    rehydrate(
        concurrent_risk_gbp=concurrent,
        daily_deployed_gbp=daily_deployed,
        daily_pnl_gbp=daily_pnl,
    )
    snap = snapshot()
    log_engine(
        f"portfolio_envelope rehydrated: concurrent=£{snap['concurrent_risk_gbp']:.0f} "
        f"daily_deploy=£{snap['daily_deployed_gbp']:.0f} "
        f"daily_pnl=£{snap['daily_pnl_gbp']:+.0f} "
        f"open_deals={len(_deal_risk_gbp)}"
    )
    return {
        "concurrent": concurrent,
        "daily_deployed": daily_deployed,
        "daily_pnl": daily_pnl,
    }


def record_portfolio_exit_for_deal(
    deal_id: str,
    *,
    pnl_gbp: float = 0.0,
    direction: str = "",
) -> None:
    try:
        from system.portfolio_envelope import portfolio_gate_enabled, record_exit

        if not portfolio_gate_enabled():
            return
        key = str(deal_id or "")
        risk = _deal_risk_gbp.pop(key, 0.0)
        if risk <= 0:
            return
        record_exit(risk, pnl_gbp=float(pnl_gbp))
        if direction:
            try:
                from execution.correlation_guard import release_direction_risk

                release_direction_risk(direction, risk)
            except Exception:
                pass
        log_engine(
            f"portfolio_envelope exit deal={deal_id} released={risk:.2f} "
            f"pnl_gbp={float(pnl_gbp):+.2f}"
        )
    except Exception as e:
        log_engine(
            f"portfolio_envelope exit hook failed deal={deal_id}: "
            f"{type(e).__name__}: {e}"
        )


def rehydrate_risk_guards_from_store(store: Any, *, cfg: Any | None = None) -> None:
    """Portfolio envelope + correlation £ heat from open trades on agent start."""
    rehydrate_portfolio_from_store(store, cfg=cfg)
    buy_risk = 0.0
    sell_risk = 0.0
    try:
        for row in store.active_trades():
            if int(row["dry_run"] or 0):
                continue
            risk = risk_gbp_from_trade_row(row, cfg=cfg)
            side = str(row["side"] or "").upper()
            if side == "BUY":
                buy_risk += risk
            elif side == "SELL":
                sell_risk += risk
    except Exception as e:
        log_engine(f"correlation_guard rehydrate: {type(e).__name__}: {e}")
    try:
        from execution.correlation_guard import rehydrate_direction_risk

        rehydrate_direction_risk(buy_risk_gbp=buy_risk, sell_risk_gbp=sell_risk)
    except Exception as e:
        log_engine(f"correlation_guard rehydrate failed: {type(e).__name__}: {e}")
