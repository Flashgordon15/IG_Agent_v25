"""
Manual position close — POST /api/close/{deal_id} only write path (Step 8).

Routes to the same IG REST close_position() used by the execution layer.
"""

from __future__ import annotations

import time
from typing import Any, Callable

CloseFn = Callable[[str], dict[str, Any]]

_handler: CloseFn | None = None
_VERIFY_WAIT_SEC = 2.0
_VERIFY_POLLS = 5


def register_close_handler(fn: CloseFn | None) -> None:
    global _handler
    _handler = fn


def reset_close_handler_for_tests() -> None:
    global _handler
    _handler = None


def close_deal(deal_id: str) -> dict[str, Any]:
    deal_id = str(deal_id or "").strip()
    if not deal_id:
        raise ValueError("deal_id required")
    if _handler is not None:
        return _handler(deal_id)
    return _default_ig_close(deal_id)


def _wait_position_closed(rest: Any, deal_id: str) -> bool:
    for _ in range(_VERIFY_POLLS):
        try:
            if hasattr(rest, "is_position_open") and not rest.is_position_open(deal_id):
                return True
            if hasattr(rest, "find_open_position") and rest.find_open_position(deal_id) is None:
                return True
        except Exception:
            pass
        time.sleep(_VERIFY_WAIT_SEC)
    return False


def _default_ig_close(deal_id: str) -> dict[str, Any]:
    from system.config_loader import ConfigLoader
    from system.credentials_loader import try_load_credentials
    from system.engine_log import log_engine
    from system.ig_rest_session import ensure_shared_authenticated
    from system.paths import config_dir

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        raise RuntimeError(status.error or "credentials missing — cannot close position")

    cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
    rest = ensure_shared_authenticated(status.credentials)
    ccy = cfg.currency_code

    targets: list[tuple[str, str, float, str]] = []
    for item in rest.open_positions():
        market = item.get("market") or {}
        pos = item.get("position") or {}
        did = str(pos.get("dealId") or "")
        if did != deal_id:
            continue
        side = str(pos.get("direction") or "BUY").upper()
        size = float(pos.get("size") or 0)
        epic = str(market.get("epic") or "")
        if size > 0:
            targets.append((did, side, size, epic))

    if not targets:
        raise LookupError(f"open position not found for deal_id={deal_id}")

    _, side, size, epic = targets[0]
    close_dir = "SELL" if side == "BUY" else "BUY"
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = rest.close_position(
                deal_id,
                direction=close_dir,
                size=size,
                epic=epic or None,
                currency_code=ccy,
                verify=True,
            )
            verified = bool(result.get("verified_closed"))
            if not verified:
                verified = _wait_position_closed(rest, deal_id)
            result["deal_id"] = deal_id
            result["status"] = "CLOSED" if verified else "CLOSING"
            result["attempt"] = attempt
            if verified:
                return result
            last_error = RuntimeError("close accepted but position still open")
        except Exception as exc:
            last_error = exc
            log_engine(
                f"close_deal attempt {attempt}/2 failed deal={deal_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < 2:
                time.sleep(1.0)

    msg = f"close failed after retry: {last_error}"
    try:
        from system.telegram_notifier import get_telegram_notifier

        notifier = get_telegram_notifier()
        if notifier and notifier.enabled:
            notifier.send(f"🔴 CLOSE FAILED — deal {deal_id}: {last_error}")
    except Exception:
        pass
    raise RuntimeError(msg) from last_error
