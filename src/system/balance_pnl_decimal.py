"""
Decimal-safe account balance / session P&L resolution for drawdown guards.

Prevents float drift (e.g. -500.03 vs -500.00) and balance vs available mismatches
from triggering false Superjet drawdown breaches.
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from system.engine_log import log_engine

_TWOPLACES = Decimal("0.01")
_ZERO = Decimal("0")


def money_decimal(value: Any, *, field: str = "value") -> Decimal | None:
    """Parse IG money fields without float intermediate when string."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text:
                return None
            return Decimal(text).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
        # float / int — quantize immediately to arrest drift
        return Decimal(str(value)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    except Exception:
        log_engine(f"balance_pnl_decimal: invalid {field}={value!r}")
        return None


def decimal_to_float(d: Decimal | None) -> float:
    if d is None:
        return 0.0
    return float(d.quantize(_TWOPLACES, rounding=ROUND_HALF_UP))


def session_pnl_decimal(
    *,
    session_start: Decimal | None,
    current_balance: Decimal | None,
) -> Decimal | None:
    if session_start is None or current_balance is None:
        return None
    return (current_balance - session_start).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def extract_ig_account_balance_block(
    account_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize IG /accounts row balance block for logging + Decimal parse."""
    if not isinstance(account_row, dict):
        return {}
    bal = account_row.get("balance")
    if not isinstance(bal, dict):
        bal = {}
    currency = str(
        account_row.get("currency")
        or account_row.get("currencyIsoCode")
        or bal.get("currency")
        or ""
    ).upper()
    balance_d = money_decimal(bal.get("balance"), field="balance.balance")
    available_d = money_decimal(bal.get("available"), field="balance.available")
    profit_loss_d = money_decimal(bal.get("profitLoss"), field="balance.profitLoss")
    deposit_d = money_decimal(bal.get("deposit"), field="balance.deposit")
    return {
        "account_id": str(account_row.get("accountId") or ""),
        "account_name": str(account_row.get("accountName") or ""),
        "currency": currency,
        "balance": decimal_to_float(balance_d),
        "available": decimal_to_float(available_d),
        "profit_loss": decimal_to_float(profit_loss_d),
        "deposit": decimal_to_float(deposit_d),
        "balance_decimal": str(balance_d) if balance_d is not None else None,
        "available_decimal": str(available_d) if available_d is not None else None,
        "profit_loss_decimal": str(profit_loss_d) if profit_loss_d is not None else None,
        "balance_available_delta": decimal_to_float(
            (balance_d - available_d) if balance_d is not None and available_d is not None else None
        ),
    }


def collect_drawdown_debug_context() -> dict[str, Any]:
    """Assemble raw API + monitor + store context immediately before drawdown rule."""
    ctx: dict[str, Any] = {"source": "balance_pnl_decimal"}

    try:
        from system.account_currency import get_account_currency

        ctx["account_currency"] = get_account_currency()
    except Exception:
        ctx["account_currency"] = "GBP"

    raw_payload: dict[str, Any] | None = None
    try:
        from api.agent_control import get_trading_loop

        loop_bundle = get_trading_loop()
        rest = None
        if loop_bundle is not None and hasattr(loop_bundle, "loops") and loop_bundle.loops:
            rest = getattr(loop_bundle.loops[0], "_rest_client", None)
        if rest is None:
            try:
                from system.credentials_loader import load_credentials
                from system.ig_rest_session import get_shared_rest_client

                rest = get_shared_rest_client(load_credentials())
            except Exception:
                rest = None
        if rest is not None and hasattr(rest, "get_last_accounts_raw_payload"):
            raw_payload = rest.get_last_accounts_raw_payload()
        if rest is not None and hasattr(rest, "get_cached_account_summary"):
            ctx["cached_account_summary"] = rest.get_cached_account_summary()
    except Exception as exc:
        ctx["rest_error"] = f"{type(exc).__name__}: {exc}"

    if raw_payload is not None:
        ctx["raw_accounts_api_payload"] = raw_payload
        accounts = raw_payload.get("accounts") if isinstance(raw_payload, dict) else None
        if isinstance(accounts, list) and accounts:
            ctx["parsed_primary_account"] = extract_ig_account_balance_block(accounts[0])
            for row in accounts:
                if isinstance(row, dict):
                    aid = str(row.get("accountId") or "")
                    if aid and aid == str(ctx.get("configured_account_id") or ""):
                        ctx["parsed_configured_account"] = extract_ig_account_balance_block(row)
                        break

    try:
        from system.drawdown_monitor import snapshot_decimal_debug

        ctx["drawdown_monitor"] = snapshot_decimal_debug()
    except Exception as exc:
        ctx["drawdown_monitor_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config
        from system.daily_loss_policy import effective_daily_pnl, daily_loss_reset_snapshot

        cfg = get_config(reload=False)
        store = LearningStore(str(cfg.learning_db))
        raw_store = store.sum_daily_pnl(__import__("datetime").date.today().isoformat())
        eff = effective_daily_pnl(store)
        ctx["learning_store"] = {
            "raw_daily_pnl": decimal_to_float(money_decimal(raw_store, field="raw_daily_pnl")),
            "effective_daily_pnl": decimal_to_float(money_decimal(eff, field="effective_daily_pnl")),
            "reset": daily_loss_reset_snapshot(store),
        }
    except Exception as exc:
        ctx["learning_store_error"] = f"{type(exc).__name__}: {exc}"

    try:
        from trading.open_position_view import position_map_from_rows

        rows: list[dict[str, Any]] = []
        from api.agent_control import get_trading_loop

        loop_bundle = get_trading_loop()
        if loop_bundle is not None and hasattr(loop_bundle, "loops"):
            for tl in loop_bundle.loops:
                sync = getattr(tl, "_position_sync", None)
                if sync is not None and hasattr(sync, "snapshot_dict"):
                    snap = sync.snapshot_dict()
                    pmap = snap.get("position_map")
                    if isinstance(pmap, dict):
                        rows.extend(pmap.values())
                    else:
                        rows.extend(snap.get("positions") or [])
        ctx["open_positions_count"] = len(position_map_from_rows(rows))
    except Exception:
        ctx["open_positions_count"] = None

    return ctx


def log_drawdown_evaluation_debug(ctx: dict[str, Any]) -> None:
    """Emit structured debug block to engine.log (Flight Deck avionics log)."""
    try:
        payload = json.dumps(ctx, default=str, separators=(",", ":"))
        if len(payload) > 6000:
            payload = payload[:6000] + "…(truncated)"
        log_engine(f"DRAWDOWN_EVAL_DEBUG raw_context={payload}")
    except Exception as exc:
        log_engine(f"DRAWDOWN_EVAL_DEBUG serialize failed: {type(exc).__name__}: {exc}")


def resolve_daily_pnl_gbp_decimal() -> tuple[Decimal, dict[str, Any]]:
    """
    Authoritative signed daily P&L (GBP) for Superjet drawdown ceiling.

    Priority:
      1. Learning store effective_daily_pnl (closed-trade truth, signed)
      2. Broker open unrealized UPL sum (signed)
      3. Session balance delta ONLY when fields are consistent (balance.balance,
         not available) — logged as diagnostic fallback
    """
    ctx = collect_drawdown_debug_context()
    components: dict[str, str] = {}

    realised = _ZERO
    try:
        from data.learning_store import LearningStore
        from system.config_loader import get_config
        from system.daily_loss_policy import effective_daily_pnl

        cfg = get_config(reload=False)
        store = LearningStore(str(cfg.learning_db))
        eff = money_decimal(effective_daily_pnl(store), field="effective_daily_pnl")
        if eff is not None:
            realised = eff
            components["realised_source"] = "learning_store.effective_daily_pnl"
    except Exception as exc:
        components["realised_error"] = f"{type(exc).__name__}: {exc}"

    unrealized = _ZERO
    try:
        from intelligence.target_engine import get_target_engine

        upl = money_decimal(
            get_target_engine().resolve_open_unrealized_gbp(),
            field="open_unrealized_gbp",
        )
        if upl is not None:
            unrealized = upl
            components["unrealized_source"] = "target_engine.resolve_open_unrealized_gbp"
    except Exception as exc:
        components["unrealized_error"] = f"{type(exc).__name__}: {exc}"

    session_delta = _ZERO
    session_delta_usable = False
    try:
        from system.drawdown_monitor import snapshot_decimal_debug

        dd = snapshot_decimal_debug()
        start_d = money_decimal(dd.get("session_start_balance_decimal"), field="session_start")
        cur_d = money_decimal(dd.get("current_balance_decimal"), field="current_balance")
        bal_field = dd.get("last_balance_field_used")
        if start_d is not None and cur_d is not None and bal_field == "balance":
            session_delta = (cur_d - start_d).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
            session_delta_usable = True
            components["session_delta_source"] = "drawdown_monitor.balance_field"
        elif start_d is not None and cur_d is not None:
            components["session_delta_skipped"] = f"last_field={bal_field!r} (not balance)"
            ctx["session_balance_mismatch_warning"] = (
                "Session P&L used available or mixed fields — excluded from breach math"
            )
    except Exception as exc:
        components["session_delta_error"] = f"{type(exc).__name__}: {exc}"

    # Prefer store realised; add unrealized. Cross-check session delta in debug only unless
    # store is empty and session delta is from balance.balance.
    total = (realised + unrealized).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    if realised == _ZERO and unrealized == _ZERO and session_delta_usable:
        total = session_delta
        components["total_source"] = "session_balance_delta_fallback"
    else:
        components["total_source"] = "store_realised_plus_unrealized"

    parsed = ctx.get("parsed_configured_account") or ctx.get("parsed_primary_account") or {}
    if isinstance(parsed, dict):
        bal_d = money_decimal(parsed.get("balance_decimal"), field="api_balance")
        avail_d = money_decimal(parsed.get("available_decimal"), field="api_available")
        if bal_d is not None and avail_d is not None:
            delta = (bal_d - avail_d).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
            if abs(delta) >= Decimal("100"):
                ctx["balance_vs_available_alert"] = {
                    "balance": str(bal_d),
                    "available": str(avail_d),
                    "delta": str(delta),
                    "note": "Large balance/available gap can cause false session P&L if mixed",
                }

    float_leak = None
    if isinstance(parsed, dict) and parsed.get("balance") is not None:
        float_bal = float(parsed["balance"])
        dec_bal = money_decimal(parsed.get("balance_decimal"), field="balance_decimal")
        if dec_bal is not None:
            leak = Decimal(str(float_bal)) - dec_bal
            if leak.copy_abs() > Decimal("0.001"):
                float_leak = {"float": float_bal, "decimal": str(dec_bal), "leak": str(leak)}

    ctx["pnl_components"] = {
        **components,
        "realised_decimal": str(realised),
        "unrealized_decimal": str(unrealized),
        "session_delta_decimal": str(session_delta),
        "total_decimal": str(total),
        "float_conversion_leak": float_leak,
    }

    log_drawdown_evaluation_debug(ctx)
    return total, ctx
