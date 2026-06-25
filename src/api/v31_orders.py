"""
v31 production-plane order fulfillment — async 202 intake → background IG REST.

POST /api/v31/orders/fulfill accepts a breakout payload, returns HTTP 202 immediately,
and dispatches broker place/confirm on a decoupled asyncio task (httpx timeout=2.0s).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from analytics.triage_db import connect_triage_sqlite
from system.engine_log import log_engine

_TRIAGE_V31_DEFAULT = Path(__file__).resolve().parents[1] / "analytics" / "triage_v31.db"
_FULFILL_HTTP_TIMEOUT_SEC = 2.0
_PENDING_TASKS: set[asyncio.Task[Any]] = set()

_PRODUCTION_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_reference TEXT NOT NULL UNIQUE,
    deal_id TEXT,
    epic TEXT NOT NULL,
    direction TEXT NOT NULL,
    size REAL NOT NULL,
    status TEXT NOT NULL,
    broker_payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_production_orders_deal_id ON production_orders(deal_id);
CREATE INDEX IF NOT EXISTS idx_production_orders_status ON production_orders(status);
"""


@dataclass(frozen=True)
class _FulfillRequest:
    signal: str
    deal_reference: str
    epic: str
    direction: str
    size: float
    stop_distance: float
    limit_distance: float | None
    currency_code: str


def _triage_v31_path() -> Path:
    raw = os.environ.get("IG_TRIAGE_DB", "").strip()
    if raw:
        return Path(raw).resolve()
    return _TRIAGE_V31_DEFAULT.resolve()


def _resolve_rest_client(boot_context: Any | None) -> Any:
    if boot_context is not None:
        client = getattr(boot_context, "rest_client", None)
        if client is not None:
            return client
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    status = try_load_credentials()
    if not status.ok or status.credentials is None:
        raise RuntimeError(status.error or "credentials not loaded")
    return ensure_shared_authenticated(status.credentials)


def _ledger_write(
    *,
    deal_reference: str,
    deal_id: str | None,
    epic: str,
    direction: str,
    size: float,
    status: str,
    broker_payload: dict[str, Any],
) -> None:
    db = _triage_v31_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_triage_sqlite(db)
    try:
        conn.executescript(_PRODUCTION_ORDERS_DDL)
        conn.execute(
            """
            INSERT OR REPLACE INTO production_orders
                (deal_reference, deal_id, epic, direction, size, status, broker_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal_reference,
                deal_id,
                epic,
                direction.upper(),
                float(size),
                status,
                json.dumps(broker_payload, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_fulfill_request(body: dict[str, Any]) -> _FulfillRequest:
    signal = str(body.get("signal") or "BREAKOUT").upper()
    if signal not in ("BREAKOUT", "FORCE", "SYNTHETIC"):
        raise ValueError(f"unsupported signal type: {signal}")

    deal_reference = str(body.get("dealReference") or body.get("deal_reference") or "").strip()
    if not deal_reference:
        raise ValueError("dealReference required")

    size = float(body.get("size") or 0.5)
    if size <= 0:
        raise ValueError("size must be positive")

    direction = str(body.get("direction") or body.get("action") or "BUY").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")

    epic = str(body.get("epic") or "").strip()
    if not epic:
        raise ValueError("epic required for async v31 fulfill")

    from system.config_loader import load_active_config

    cfg = load_active_config(validate=False)
    stop_distance = float(
        body.get("trailing_stop_distance")
        or body.get("stop_distance")
        or body.get("stopDistance")
        or getattr(cfg, "stop_distance_points", 10.0)
    )
    limit_distance = (
        body.get("scalp_limit")
        or body.get("scalp_limit_points")
        or body.get("limit_distance")
        or body.get("limitDistance")
    )
    if limit_distance is None and cfg is not None:
        limit_distance = float(getattr(cfg, "limit_distance_points", stop_distance * 2))
    currency_code = str(
        body.get("currency_code")
        or body.get("currencyCode")
        or getattr(cfg, "currency_code", "USD")
    )

    return _FulfillRequest(
        signal=signal,
        deal_reference=deal_reference,
        epic=epic,
        direction=direction,
        size=size,
        stop_distance=stop_distance,
        limit_distance=float(limit_distance) if limit_distance is not None else None,
        currency_code=currency_code,
    )


async def accept_v31_breakout_order(
    body: dict[str, Any],
    *,
    boot_context: Any | None = None,
) -> dict[str, Any]:
    """
    Validate intake, persist ACCEPTED ledger row, spawn background broker task.
    Returns immediately — never blocks on IG REST.
    """
    req = _parse_fulfill_request(body)
    os.environ["IG_V31_FORCE_DEMO_TRADE"] = "1"

    accepted = {
        "ok": True,
        "status": "ACCEPTED",
        "async": True,
        "dealReference": req.deal_reference,
        "epic": req.epic,
        "direction": req.direction,
        "size": req.size,
        "signal": req.signal,
    }
    try:
        from execution.broker_wire_handshake import broker_wire_handshake_log_path

        accepted["wire_handshake_log"] = str(broker_wire_handshake_log_path())
    except Exception:
        pass

    try:
        _ledger_write(
            deal_reference=req.deal_reference,
            deal_id=None,
            epic=req.epic,
            direction=req.direction,
            size=req.size,
            status="ACCEPTED",
            broker_payload={"phase": "accepted", "async": True},
        )
    except Exception as exc:
        accepted["ledger_error"] = f"{type(exc).__name__}: {exc}"

    task = asyncio.create_task(
        _fulfill_background(req, boot_context=boot_context),
        name=f"v31-fulfill-{req.deal_reference[:16]}",
    )
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)

    log_engine(
        f"V31_FULFILL accepted async: {req.direction} {req.epic} "
        f"size={req.size:g} dealReference={req.deal_reference}"
    )
    return accepted


async def _fulfill_background(req: _FulfillRequest, *, boot_context: Any | None) -> None:
    """Background broker loop — httpx hard timeout sheds hung connections."""
    t0 = time.perf_counter()
    try:
        receipt = await _execute_broker_fulfill_async(req, boot_context=boot_context)
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        receipt["latency_ms"] = latency_ms
        terminal_status = str(receipt.get("status") or "REJECTED")
        deal_id = receipt.get("dealId")
        broker_ref = str(receipt.get("dealReference") or req.deal_reference)

        _ledger_write(
            deal_reference=broker_ref,
            deal_id=str(deal_id) if deal_id else None,
            epic=req.epic,
            direction=req.direction,
            size=req.size,
            status=terminal_status,
            broker_payload=receipt.get("broker") or {},
        )
        log_engine(
            f"V31_FULFILL receipt status={terminal_status} dealId={deal_id or '—'} "
            f"latency_ms={latency_ms}"
        )
    except httpx.TimeoutException:
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        log_engine(
            f"V31_FULFILL timeout shed dealReference={req.deal_reference} "
            f"after {latency_ms}ms (httpx {_FULFILL_HTTP_TIMEOUT_SEC}s)"
        )
        _ledger_write(
            deal_reference=req.deal_reference,
            deal_id=None,
            epic=req.epic,
            direction=req.direction,
            size=req.size,
            status="TIMEOUT",
            broker_payload={"error": "httpx_timeout", "timeout_sec": _FULFILL_HTTP_TIMEOUT_SEC},
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        log_engine(
            f"V31_FULFILL background error dealReference={req.deal_reference}: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            from execution.broker_error_log import append_broker_rejection

            append_broker_rejection(
                source="v31_orders._fulfill_background",
                epic=req.epic,
                direction=req.direction,
                exception_type=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
        _ledger_write(
            deal_reference=req.deal_reference,
            deal_id=None,
            epic=req.epic,
            direction=req.direction,
            size=req.size,
            status="FAILED",
            broker_payload={"error": f"{type(exc).__name__}: {exc}", "latency_ms": latency_ms},
        )


async def _execute_broker_fulfill_async(
    req: _FulfillRequest,
    *,
    boot_context: Any | None,
) -> dict[str, Any]:
    """Place + single-shot confirm via httpx.AsyncClient(timeout=2.0)."""
    from ig_api.endpoints import position_otc

    def _prepare_sync() -> tuple[Any, float, float | None, Any]:
        rest = _resolve_rest_client(boot_context)
        account_type = str(getattr(rest, "account_type", "") or "").upper()
        if account_type and account_type != "DEMO":
            raise RuntimeError(
                f"v31 fulfill blocked — account_type={account_type} (DEMO required)"
            )
        rest.login()
        from execution.live_broker_order_router import normalize_placement_distances

        stop_distance, limit_distance, stop_res = normalize_placement_distances(
            rest,
            req.epic,
            stop_distance=req.stop_distance,
            limit_distance=req.limit_distance,
        )
        return rest, stop_distance, limit_distance, stop_res

    rest, stop_distance, limit_distance, stop_res = await asyncio.to_thread(_prepare_sync)

    from execution.broker_epic_resolver import resolve_account_product, resolve_order_epic

    broker_epic = resolve_order_epic(req.epic, account_product=resolve_account_product(rest=rest))
    if broker_epic != req.epic:
        log_engine(f"V31_FULFILL: epic remap {req.epic} → {broker_epic}")

    payload: dict[str, Any] = {
        "epic": broker_epic,
        "expiry": "-",
        "direction": req.direction.upper(),
        "size": float(req.size),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": req.currency_code,
        "stopDistance": float(stop_distance),
    }
    if limit_distance is not None and float(limit_distance) > 0:
        payload["limitDistance"] = float(limit_distance)

    base = str(getattr(rest, "_base", "") or "").rstrip("/")
    place_url = f"{base}{position_otc()}"
    place_headers = rest._auth_headers("2")
    confirm_headers = rest._auth_headers("1")

    log_engine(
        f"V31_FULFILL: {req.direction} {req.epic} size={req.size:g} "
        f"dealReference={req.deal_reference}"
    )

    timeout = httpx.Timeout(_FULFILL_HTTP_TIMEOUT_SEC)
    async with httpx.AsyncClient(timeout=timeout) as client:
        place_resp = await client.post(place_url, json=payload, headers=place_headers)
        if place_resp.status_code not in (200, 201):
            body_text = (place_resp.text or "")[:2000]
            parsed: dict[str, Any] | None = None
            try:
                parsed = place_resp.json()
            except Exception:
                parsed = None
            from execution.broker_error_log import append_broker_rejection

            append_broker_rejection(
                source="v31_orders._execute_broker_fulfill_async",
                epic=broker_epic,
                direction=req.direction,
                payload=payload,
                response_body=parsed if parsed is not None else body_text,
                status_code=place_resp.status_code,
                message=f"place HTTP {place_resp.status_code}",
            )
            from execution.broker_wire_handshake import append_broker_wire_handshake

            append_broker_wire_handshake(
                source="v31_orders._execute_broker_fulfill_async",
                phase="place_rejected",
                epic=broker_epic,
                direction=req.direction,
                request_payload=payload,
                response_text=body_text,
                response_json=parsed,
                status_code=place_resp.status_code,
                ok=False,
            )
            raise RuntimeError(f"place HTTP {place_resp.status_code}: {body_text[:300]}")
        place_body = place_resp.json()
        from execution.broker_wire_handshake import append_broker_wire_handshake

        append_broker_wire_handshake(
            source="v31_orders._execute_broker_fulfill_async",
            phase="place_ok",
            epic=broker_epic,
            direction=req.direction,
            request_payload=payload,
            response_text=place_resp.text or "",
            response_json=place_body if isinstance(place_body, dict) else None,
            status_code=place_resp.status_code,
            ok=True,
            message=str(place_body.get("dealReference") or ""),
        )
        broker_ref = str(place_body.get("dealReference") or req.deal_reference)

        confirm_url = f"{base}/confirms/{broker_ref}"
        confirm_resp = await client.get(confirm_url, headers=confirm_headers)
        confirm_body: dict[str, Any] = {}
        if confirm_resp.status_code == 200:
            confirm_body = confirm_resp.json()
            append_broker_wire_handshake(
                source="v31_orders._execute_broker_fulfill_async",
                phase="confirm_ok",
                epic=broker_epic,
                direction=req.direction,
                request_payload={"dealReference": broker_ref},
                response_text=confirm_resp.text or "",
                response_json=confirm_body if isinstance(confirm_body, dict) else None,
                status_code=confirm_resp.status_code,
                ok=str(confirm_body.get("dealStatus") or "").upper() == "ACCEPTED",
                message=str(confirm_body.get("dealStatus") or ""),
            )

    confirm_status = str(
        confirm_body.get("dealStatus") or confirm_body.get("status") or ""
    ).upper()
    deal_id = confirm_body.get("dealId") or place_body.get("dealId")
    accepted = confirm_status == "ACCEPTED"

    if accepted:
        terminal_status = "CONFIRMED"
    elif confirm_status == "REJECTED":
        terminal_status = "REJECTED"
        try:
            from execution.broker_error_log import append_broker_rejection

            append_broker_rejection(
                source="v31_orders.confirm_rejected",
                epic=broker_epic,
                direction=req.direction,
                payload=payload,
                response_body=confirm_body,
                message=str(
                    confirm_body.get("reason")
                    or confirm_body.get("reasonCode")
                    or confirm_body.get("errorCode")
                    or "confirm REJECTED"
                ),
            )
        except Exception:
            pass
    elif place_body.get("dealReference"):
        terminal_status = "EXECUTED"
    else:
        terminal_status = "REJECTED"

    return {
        "ok": terminal_status in ("EXECUTED", "CONFIRMED"),
        "status": terminal_status,
        "dealReference": broker_ref,
        "dealId": deal_id,
        "epic": req.epic,
        "direction": req.direction,
        "size": req.size,
        "signal": req.signal,
        "stop_distance_resolution": {
            "requested_points": stop_res.requested_points,
            "min_points": stop_res.min_points,
            "effective_points": stop_res.effective_points,
        },
        "place": place_body,
        "confirm": confirm_body,
        "broker": {
            "dealReference": broker_ref,
            "dealId": deal_id,
            "dealStatus": confirm_status or terminal_status,
            "accepted": accepted,
            "reason": str(
                confirm_body.get("reason")
                or confirm_body.get("reasonCode")
                or confirm_body.get("errorCode")
                or ""
            ),
            "raw_confirm": confirm_body,
            "raw_place": place_body,
        },
    }


def fulfill_v31_breakout_order(
    body: dict[str, Any],
    *,
    boot_context: Any | None = None,
) -> dict[str, Any]:
    """Synchronous shim for scripts — runs the async broker path to completion."""
    req = _parse_fulfill_request(body)
    os.environ["IG_V31_FORCE_DEMO_TRADE"] = "1"
    return asyncio.run(_execute_broker_fulfill_async(req, boot_context=boot_context))
