"""
Manual intervention engine — administrative overrides outside the trailing loop.

Provides force-close, force-breakeven, and a daily drawdown safety shield that
hard-blocks new entries until midnight reset without altering trailing logic.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from system.engine_log import log_engine

SHIELD_BREACH_KEY = "daily_max_loss_breached"
SHIELD_BREACH_DAY_KEY = "daily_max_loss_breached_day"
SHIELD_BREACH_AT_KEY = "daily_max_loss_breached_at"
SHIELD_LOSS_KEY = "daily_max_loss_breached_loss_gbp"


def _today() -> str:
    return date.today().isoformat()


def shield_threshold_gbp(cfg: Any | None = None) -> float:
    """Configurable closed-day loss threshold for the admin safety shield."""
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return 500.0
    try:
        block = cfg.get("manual_intervention") or {}
        if isinstance(block, dict) and block.get("daily_drawdown_shield_gbp") is not None:
            return float(block["daily_drawdown_shield_gbp"])
    except (TypeError, ValueError, AttributeError):
        pass
    return 500.0


def shield_enabled(cfg: Any | None = None) -> bool:
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            return True
    try:
        block = cfg.get("manual_intervention") or {}
        if isinstance(block, dict) and "daily_drawdown_shield_enabled" in block:
            return bool(block["daily_drawdown_shield_enabled"])
    except (TypeError, ValueError, AttributeError):
        pass
    return True


def _resolve_store(store: Any | None) -> Any | None:
    if store is not None:
        return store
    try:
        from system.config_loader import get_config
        from data.learning_store import LearningStore

        cfg = get_config()
        s = LearningStore(str(cfg.learning_db))
        s.connect()
        return s
    except Exception:
        return None


def _resolve_rest(rest: Any | None = None) -> Any:
    if rest is not None:
        return rest
    from system.config_loader import ConfigLoader
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated
    from system.paths import config_dir

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        raise RuntimeError(status.error or "credentials missing")
    return ensure_shared_authenticated(status.credentials)


def _resolve_cfg(cfg: Any | None = None) -> Any:
    if cfg is not None:
        return cfg
    from system.config_loader import ConfigLoader
    from system.paths import config_dir

    return ConfigLoader(config_dir() / "config_v25.json").load_config()


def _clear_shield_breach(store: Any) -> None:
    store.set_runtime_state(SHIELD_BREACH_KEY, "0")
    store.set_runtime_state(SHIELD_BREACH_DAY_KEY, "")
    store.set_runtime_state(SHIELD_BREACH_AT_KEY, "")
    store.set_runtime_state(SHIELD_LOSS_KEY, "0")


def _maybe_reset_shield_for_new_day(store: Any | None) -> None:
    if store is None:
        return
    today = _today()
    breach_day = store.get_runtime_state(SHIELD_BREACH_DAY_KEY) or ""
    if breach_day and breach_day != today:
        _clear_shield_breach(store)


def refresh_daily_drawdown_shield(
    store: Any | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """
    Evaluate closed-day P&L and latch ``daily_max_loss_breached`` when threshold exceeded.
    Resets automatically after midnight (new calendar day).
    """
    from system.daily_loss_policy import effective_daily_loss_gbp

    store = _resolve_store(store)
    cfg = _resolve_cfg(cfg)
    threshold = shield_threshold_gbp(cfg)
    enabled = shield_enabled(cfg)
    today = _today()

    if store is None:
        return {
            "enabled": enabled,
            "threshold_gbp": threshold,
            "closed_loss_gbp": 0.0,
            "daily_max_loss_breached": False,
            "detail": "store unavailable",
        }

    _maybe_reset_shield_for_new_day(store)
    closed_loss = effective_daily_loss_gbp(store, day=today)
    breached = daily_max_loss_breached(store)

    if enabled and not breached and closed_loss >= threshold:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.set_runtime_state(SHIELD_BREACH_KEY, "1")
        store.set_runtime_state(SHIELD_BREACH_DAY_KEY, today)
        store.set_runtime_state(SHIELD_BREACH_AT_KEY, now)
        store.set_runtime_state(SHIELD_LOSS_KEY, f"{closed_loss:.2f}")
        breached = True
        log_engine(
            f"Daily Drawdown Safety Shield TRIPPED — closed loss £{closed_loss:.2f} "
            f">= £{threshold:.0f} (entries blocked until midnight)"
        )
        try:
            from system.telegram_notifier import send_critical_alert

            send_critical_alert(
                f"🛡️ Daily drawdown shield — closed loss £{closed_loss:.2f} "
                f">= £{threshold:.0f} (entries blocked until midnight)"
            )
        except Exception as exc:
            log_engine(
                f"telegram drawdown-shield alert failed: {type(exc).__name__}: {exc}"
            )

    detail = (
        f"shield tripped — closed loss £{closed_loss:.2f} >= £{threshold:.0f}"
        if breached
        else f"closed loss £{closed_loss:.2f} < £{threshold:.0f}"
    )
    return {
        "enabled": enabled,
        "threshold_gbp": threshold,
        "closed_loss_gbp": round(closed_loss, 2),
        "daily_max_loss_breached": breached,
        "breach_day": store.get_runtime_state(SHIELD_BREACH_DAY_KEY) or "",
        "breach_at": store.get_runtime_state(SHIELD_BREACH_AT_KEY) or "",
        "detail": detail,
    }


def daily_max_loss_breached(store: Any | None = None) -> bool:
    """True when the admin drawdown shield has latched for the current day."""
    store = _resolve_store(store)
    if store is None:
        return False
    _maybe_reset_shield_for_new_day(store)
    return store.get_runtime_state(SHIELD_BREACH_KEY) == "1"


def entries_blocked_by_shield(
    store: Any | None = None,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """Returns (blocked, reason) for entry gates."""
    if not shield_enabled(cfg):
        return False, ""
    store = _resolve_store(store)
    if store is None:
        return False, ""
    _maybe_reset_shield_for_new_day(store)
    if daily_max_loss_breached(store):
        loss_raw = store.get_runtime_state(SHIELD_LOSS_KEY) or "0"
        try:
            loss = float(loss_raw or 0)
        except (TypeError, ValueError):
            loss = 0.0
        threshold = shield_threshold_gbp(cfg)
        return (
            True,
            f"daily drawdown shield — closed loss £{loss:.2f} >= £{threshold:.0f} "
            f"(entries blocked until midnight)",
        )
    refresh_daily_drawdown_shield(store, cfg)
    if not daily_max_loss_breached(store):
        return False, ""
    loss_raw = store.get_runtime_state(SHIELD_LOSS_KEY) or "0"
    try:
        loss = float(loss_raw or 0)
    except (TypeError, ValueError):
        loss = 0.0
    threshold = shield_threshold_gbp(cfg)
    return (
        True,
        f"daily drawdown shield — closed loss £{loss:.2f} >= £{threshold:.0f} "
        f"(entries blocked until midnight)",
    )


def risk_status(
    store: Any | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """Aggregate admin risk view for /api/admin/risk-status."""
    from system.daily_loss_policy import daily_loss_gate_status

    store = _resolve_store(store)
    cfg = _resolve_cfg(cfg)
    shield = refresh_daily_drawdown_shield(store, cfg)
    loss_ok, loss_detail, loss_meta = daily_loss_gate_status(store, cfg)
    blocked = bool(shield.get("daily_max_loss_breached"))
    block_reason = None
    if blocked:
        threshold = shield_threshold_gbp(cfg)
        loss = float(shield.get("closed_loss_gbp") or 0)
        block_reason = (
            f"daily drawdown shield — closed loss £{loss:.2f} >= £{threshold:.0f} "
            f"(entries blocked until midnight)"
        )
    return {
        "ok": True,
        "daily_max_loss_breached": shield["daily_max_loss_breached"],
        "entries_blocked_by_shield": blocked,
        "shield": shield,
        "daily_loss_gate": {
            "passed": loss_ok,
            "detail": loss_detail,
            **loss_meta,
        },
        "entry_block_reason": block_reason,
    }


def force_terminate_position(
    epic: str,
    *,
    rest: Any | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """
    Immediately close all IG positions for ``epic``.

    Clears exit-inflight guards and sends MARKET close orders without trailing delays.
    """
    from execution.exit_inflight import clear_exit

    epic_key = str(epic or "").strip()
    if not epic_key:
        raise ValueError("epic required")

    rest = _resolve_rest(rest)
    cfg = _resolve_cfg(cfg)
    clear_exit(epic_key)

    closed: list[str] = []
    errors: list[str] = []
    for item in rest.open_positions():
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        deal_id = str(pos.get("dealId") or pos.get("dealID") or "")
        pos_epic = str(mkt.get("epic") or "")
        if pos_epic != epic_key:
            continue
        side = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        if not deal_id or size <= 0:
            continue
        close_dir = "SELL" if side == "BUY" else "BUY"
        try:
            result = rest._do_close_position(
                deal_id,
                direction=close_dir,
                size=size,
                epic=epic_key,
                currency_code=cfg.currency_code,
                verify=True,
            )
            closed.append(deal_id)
            log_engine(
                f"manual force-close: {epic_key} deal={deal_id} "
                f"verified={bool(result.get('verified_closed'))}"
            )
        except Exception as exc:
            msg = f"{deal_id}: {exc}"
            errors.append(msg)
            log_engine(f"manual force-close error {epic_key} {msg}")

    if not closed and not errors:
        raise LookupError(f"no open IG position for epic={epic_key}")

    return {
        "ok": len(errors) == 0,
        "epic": epic_key,
        "closed_deal_ids": closed,
        "errors": errors,
        "count": len(closed),
    }


def force_breakeven_now(
    epic: str,
    *,
    rest: Any | None = None,
    cfg: Any | None = None,
) -> dict[str, Any]:
    """
    Move local stop to entry for all active trades on ``epic`` and sync to IG immediately.

    Bypasses standard breakeven threshold checks in the trailing loop.
    """
    from execution.position_protect_hub import get_trade_manager

    epic_key = str(epic or "").strip()
    if not epic_key:
        raise ValueError("epic required")

    _resolve_rest(rest)
    _resolve_cfg(cfg)
    mgr = get_trade_manager(epic_key)
    if mgr is None:
        raise LookupError(f"no trade manager registered for epic={epic_key}")

    rest = _resolve_rest(rest)
    updated: list[dict[str, Any]] = []
    errors: list[str] = []
    for tr in mgr.store.active_trades(epic_key):
        trade_id = int(tr["id"])
        side = str(tr["side"])
        entry = float(tr["entry"])
        ig_deal = str(tr["ig_deal_id"] or "") if "ig_deal_id" in tr.keys() else ""
        be_stop = mgr._round_stop_level(entry, epic_key)
        mgr.store.update_stop(
            trade_id,
            be_stop,
            f" | Manual breakeven override stop {be_stop:.5f}",
        )
        synced = False
        if ig_deal:
            synced = mgr._execute_stop_sync(
                ig_deal,
                trade_id=trade_id,
                side=side,
                stop=be_stop,
                epic=epic_key,
            )
        updated.append(
            {
                "trade_id": trade_id,
                "deal_id": ig_deal or None,
                "entry": entry,
                "stop": be_stop,
                "synced_to_ig": synced,
            }
        )
        log_engine(
            f"manual force-breakeven: epic={epic_key} trade={trade_id} "
            f"stop={be_stop:.5f} synced={synced}"
        )

    if not updated:
        for item in rest.open_positions():
            pos = item.get("position") or {}
            mkt = item.get("market") or {}
            pos_epic = str(mkt.get("epic") or "")
            if pos_epic != epic_key:
                continue
            deal_id = str(pos.get("dealId") or pos.get("dealID") or "")
            side = str(pos.get("direction") or "BUY").upper()
            entry = float(pos.get("level") or pos.get("openLevel") or 0)
            if not deal_id or entry <= 0:
                continue
            be_stop = mgr._round_stop_level(entry, epic_key)
            synced = mgr._execute_stop_sync(
                deal_id,
                trade_id=0,
                side=side,
                stop=be_stop,
                epic=epic_key,
            )
            updated.append(
                {
                    "trade_id": None,
                    "deal_id": deal_id,
                    "entry": entry,
                    "stop": be_stop,
                    "synced_to_ig": synced,
                    "source": "ig_position_array",
                }
            )
            log_engine(
                f"manual force-breakeven (IG): epic={epic_key} deal={deal_id} "
                f"stop={be_stop:.5f} synced={synced}"
            )

    if not updated:
        raise LookupError(f"no active local trades for epic={epic_key}")

    return {
        "ok": len(errors) == 0,
        "epic": epic_key,
        "updated": updated,
        "errors": errors,
        "count": len(updated),
    }
