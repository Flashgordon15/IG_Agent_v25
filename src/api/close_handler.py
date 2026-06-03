"""
Manual position close — POST /api/close/{deal_id} only write path (Step 8).

Routes to the same IG REST close_position() used by the execution layer.
"""

from __future__ import annotations

from typing import Any, Callable

CloseFn = Callable[[str], dict[str, Any]]

_handler: CloseFn | None = None


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


def _default_ig_close(deal_id: str) -> dict[str, Any]:
    from system.config_loader import ConfigLoader
    from system.credentials_loader import try_load_credentials
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
    result = rest.close_position(
        deal_id,
        direction=close_dir,
        size=size,
        epic=epic or None,
        currency_code=ccy,
        verify=True,
    )
    result["deal_id"] = deal_id
    return result
