"""
Dry-Run Target Shadow Tracer — bare-metal execution path probe (no live placement).

Builds the IG order JSON from the 32-byte strategy slice and runs
``validate_order_schema`` against the real REST client.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

# Phase 4 extended error categories (SHM + cockpit)
AUTH_EXPIRY = "AUTH_EXPIRY"
RATE_WALL = "RATE_WALL"
REGIME_MISMATCH = "REGIME_MISMATCH"
MARGIN_LOCK = "MARGIN_LOCK"
ROUTE_OPEN = "ROUTE_OPEN"
SCHEMA_INVALID = "SCHEMA_INVALID"
CLIENT_MISSING = "CLIENT_MISSING"


def shadow_tracer_enabled() -> bool:
    if os.environ.get("IG_SHADOW_TRACER", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def _quote_age_sec(quote: Any) -> float:
    try:
        qt = getattr(quote, "time", None)
        if qt is None:
            return 0.0
        if isinstance(qt, datetime):
            if qt.tzinfo is None:
                qt = qt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - qt).total_seconds())
    except Exception:
        pass
    return 0.0


def build_shadow_order_payload(
    *,
    epic: str,
    direction: str,
    size: float,
    stop_points: float,
    limit_points: float,
    coordinate: int,
    strategy: dict[str, Any],
    quote: Any,
    market: str = "",
) -> dict[str, Any]:
    """Construct the precise IG REST OTC payload + tracer metadata."""
    bid = float(getattr(quote, "bid", 0) or 0)
    offer = float(getattr(quote, "offer", 0) or 0)
    age = _quote_age_sec(quote)
    return {
        "epic": str(epic),
        "expiry": "-",
        "direction": str(direction).upper(),
        "size": float(size),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": "GBP",
        "stopDistance": float(stop_points),
        "limitDistance": float(limit_points) if limit_points > 0 else None,
        "shadow_tracer": True,
        "coordinate": int(coordinate),
        "strategy_slice": dict(strategy),
        "market": market,
        "quote_bid": bid,
        "quote_offer": offer,
        "quote_age_sec": age,
    }


def execute_shadow_dry_run(
    *,
    loop: Any,
    quote: Any,
    epic: str,
    market: str,
    direction: str,
    coordinate: int,
    strategy: Any,
    trade_size: float,
    stop_pts: float,
    limit_pts: float,
    win_zone: bool,
) -> dict[str, Any]:
    """
    Dry-run pass — validate broker route without placing an order.
    Writes ``phase4_extended_error`` into StringPhaseDiag SHM.
    """
    if not shadow_tracer_enabled():
        return {"skipped": True}

    t0 = time.perf_counter_ns()
    strategy_dict = strategy.as_dict() if hasattr(strategy, "as_dict") else dict(strategy or {})
    payload = build_shadow_order_payload(
        epic=epic,
        direction=direction,
        size=trade_size,
        stop_points=stop_pts,
        limit_points=limit_pts,
        coordinate=coordinate,
        strategy=strategy_dict,
        quote=quote,
        market=market,
    )

    client = None
    try:
        client = loop._rest_client() if hasattr(loop, "_rest_client") else None
    except Exception:
        client = None

    result: dict[str, Any]
    if client is None or not hasattr(client, "validate_order_schema"):
        result = {
            "ok": False,
            "category": CLIENT_MISSING,
            "error": "IG REST client unavailable on execution engine",
            "payload": payload,
        }
    else:
        try:
            result = client.validate_order_schema(
                payload,
                full_session_ping=bool(win_zone),
            )
        except Exception as exc:
            result = {
                "ok": False,
                "category": _classify_exception(exc),
                "error": str(exc),
                "payload": payload,
            }
            try:
                if hasattr(loop, "_bare_metal_schedule_socket_rebind"):
                    loop._bare_metal_schedule_socket_rebind()
            except Exception:
                pass

    latency_us = int((time.perf_counter_ns() - t0) / 1000)
    if result.get("throttled"):
        category = ROUTE_OPEN
        route_open = True
        detail = str(result.get("detail") or "IG validate throttled 1500ms — RAM pass")
    else:
        category = str(result.get("category") or (ROUTE_OPEN if result.get("ok") else SCHEMA_INVALID))
        detail = str(result.get("error") or category)
        route_open = bool(result.get("ok"))

    try:
        from system.ipc.ring_buffer import _string_diag_view
        from system.ipc.string_diagnostics import record_shadow_phase4

        diag = _string_diag_view(create=True)
        if diag is not None:
            record_shadow_phase4(
                diag,
                route_open=route_open,
                category=category,
                detail=detail,
                latency_us=latency_us,
                http_status=int(result.get("http_status") or 0),
            )
    except Exception:
        pass

    if os.environ.get("IG_SHADOW_TRACER_LOG", "").strip() == "1":
        try:
            from system.engine_log import log_engine

            status = "ROUTE_OPEN" if route_open else "BLOCKED"
            log_engine(
                f"SHADOW_TRACER {status} epic={epic} {category} "
                f"coord={coordinate} payload={json.dumps(payload, default=str)[:240]}"
            )
        except Exception:
            pass

    return {
        "ok": route_open,
        "category": category,
        "detail": detail,
        "latency_us": latency_us,
        "payload": payload,
    }


def _classify_exception(exc: BaseException) -> str:
    text = str(exc).lower()
    if "401" in text or "403" in text or "auth" in text or "session" in text or "token" in text:
        return AUTH_EXPIRY
    if "429" in text or "rate" in text or "budget" in text:
        return RATE_WALL
    if "stale" in text or "quote" in text and "age" in text or "regime" in text:
        return REGIME_MISMATCH
    if "margin" in text or "insufficient" in text or "fund" in text:
        return MARGIN_LOCK
    return SCHEMA_INVALID
