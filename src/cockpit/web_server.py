"""Lightweight local web cockpit — WebSocket telemetry hub (2.5 Hz)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from system.engine_log import log_engine
from system.supervisor_history import sanitize_for_ws_json

DEFAULT_COCKPIT_PORT = 8787


def _cockpit_port() -> int:
    import os

    try:
        return int(os.environ.get("IG_COCKPIT_PORT", str(DEFAULT_COCKPIT_PORT)))
    except (TypeError, ValueError):
        return DEFAULT_COCKPIT_PORT
_server: Any | None = None
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_stop = threading.Event()
_lock = threading.Lock()

# Shared latest payload for multi-client fan-out without re-draining queue per client
_latest_payload: dict[str, Any] = {}
_latest_lock = threading.Lock()
_hot_reload_pending = False
_hot_reload_lock = threading.Lock()
_log_tail_offset = 0
_log_tail_lock = threading.Lock()

_BROKER_CLOSED_CACHE_TTL_SEC = 600.0
_broker_closed_rows: list[dict[str, Any]] = []
_broker_closed_fetched_at: float = 0.0
_broker_closed_lock = threading.Lock()
_broker_closed_bootstrapped = False
_flight_deck_boot_seeded = False


def _encode_ws_json(payload: Any) -> str:
    """Serialize a WebSocket frame — Decimal-safe, no NaN leakage."""
    clean = sanitize_for_ws_json(payload)
    return json.dumps(clean, default=str, allow_nan=False)


async def _ws_send_json_frame(ws: WebSocket, payload: Any, *, channel: str) -> bool:
    """Send one JSON frame; log serialize/send failures without killing the feed loop."""
    try:
        text = _encode_ws_json(payload)
    except (TypeError, ValueError) as exc:
        log_engine(
            f"Flight Deck {channel} WS serialize error: {type(exc).__name__}: {exc}"
        )
        return False
    try:
        await ws.send_text(text)
        return True
    except WebSocketDisconnect:
        raise
    except Exception as exc:
        log_engine(f"Flight Deck {channel} WS send error: {type(exc).__name__}: {exc}")
        return False


def cockpit_web_root() -> Path:
    from system.paths import project_root

    return project_root() / "cockpit-web"


def _drain_telemetry_queue() -> dict[str, Any] | None:
    from cockpit.telemetry_bridge import get_telemetry_queue

    tq = get_telemetry_queue()
    latest: dict[str, Any] | None = None
    try:
        while True:
            latest = tq.get_nowait()
    except queue.Empty:
        pass
    except Exception:
        return None
    if latest is not None:
        with _latest_lock:
            _latest_payload.clear()
            _latest_payload.update(latest)
    return latest


def get_latest_telemetry() -> dict[str, Any]:
    _drain_telemetry_queue()
    with _latest_lock:
        return dict(_latest_payload)


def _build_cockpit_controls_payload() -> dict[str, Any]:
    """Operator control flags — test gate forces unlocked state for Flight Deck toggles."""
    from system.protective_learning import (
        build_test_mode_cockpit_controls,
        cockpit_controls_unlocked_for_test,
    )

    try:
        from system.shutdown_cleanup import manual_stop_active

        manual = manual_stop_active()
    except Exception:
        manual = False

    if cockpit_controls_unlocked_for_test():
        return build_test_mode_cockpit_controls()

    locked = bool(manual)
    return {
        "manual_stop": locked,
        "disabled": locked,
        "controls_locked": locked,
        "shadow_toggle_enabled": not locked,
        "init_complete": True,
        "test_mode_unlock": False,
    }


def _enrich_telemetry_for_ui(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge cockpit control state and test-mode scalping overrides for Flight Deck."""
    global _flight_deck_boot_seeded
    from system.protective_learning import (
        apply_test_mode_scalping_telemetry,
        build_production_autonomous_boot_controls,
        build_test_mode_cockpit_controls,
        ensure_autonomous_engine_on_boot,
        temporary_test_gate_active,
    )

    ensure_autonomous_engine_on_boot()
    if temporary_test_gate_active():
        controls = build_test_mode_cockpit_controls()
    elif not _flight_deck_boot_seeded:
        _flight_deck_boot_seeded = True
        controls = build_production_autonomous_boot_controls()
    else:
        controls = _build_cockpit_controls_payload()
    out = dict(payload)
    out["cockpit_controls"] = controls
    if temporary_test_gate_active():
        out["test_mode_active"] = True
    shadow = dict(out.get("shadow_trading") or {})
    shadow["controls"] = controls
    shadow["manual_stop"] = False if controls.get("test_mode_unlock") else controls["manual_stop"]
    shadow["disabled"] = False if controls.get("test_mode_unlock") else controls["disabled"]
    if controls.get("autonomous_engine_on") and not shadow.get("disabled"):
        shadow["mode"] = "SHADOW"
    shadow["shadow_toggle_enabled"] = controls["shadow_toggle_enabled"]
    out["shadow_trading"] = shadow
    out["scalping_telemetry"] = apply_test_mode_scalping_telemetry(
        out.get("scalping_telemetry")
    )
    out = _normalize_positions_for_ui(out)
    try:
        from cockpit.avionics_markets import package_avionics_hud_broadcast

        out = package_avionics_hud_broadcast(out)
    except Exception as exc:
        log_engine(
            f"Flight Deck avionics HUD package skipped: {type(exc).__name__}: {exc}"
        )
    return _attach_closed_trades_for_ui(out)


def _normalize_position_row_for_ui(row: dict[str, Any]) -> dict[str, Any]:
    """Derive SELL/BUY from signed IG size and compute floating P&L for cockpit grid."""
    out = dict(row)
    try:
        size_raw = float(out.get("size") or out.get("dealSize") or 0)
    except (TypeError, ValueError):
        size_raw = 0.0

    side = str(out.get("side") or out.get("direction") or "").strip().upper()
    if size_raw < 0:
        side = "SELL"
        signed_size = size_raw
        out["size"] = abs(size_raw)
    elif side in ("SELL", "SHORT"):
        signed_size = -abs(size_raw) if size_raw else -1.0
        side = "SELL"
    elif size_raw > 0:
        signed_size = size_raw
        if side not in ("SELL", "SHORT"):
            side = "BUY"
    else:
        signed_size = 0.0
        if side not in ("BUY", "SELL", "SHORT"):
            side = ""

    out["side"] = side
    out["direction"] = side
    out["signed_size"] = signed_size

    try:
        entry = float(out.get("entry") or out.get("level") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        latest = float(
            out.get("current")
            or out.get("market")
            or out.get("mkt")
            or out.get("broker_mark")
            or 0
        )
    except (TypeError, ValueError):
        latest = 0.0

    broker_pnl = out.get("profitAndLoss")
    if broker_pnl is None:
        broker_pnl = out.get("pnl_gbp")
    if broker_pnl is None:
        broker_pnl = out.get("pnl_currency")
    if broker_pnl is None:
        broker_pnl = out.get("upl")

    floating_pnl: float | None = None
    if broker_pnl is not None:
        try:
            floating_pnl = float(broker_pnl)
        except (TypeError, ValueError):
            floating_pnl = None

    if floating_pnl is None and entry > 0 and latest > 0 and signed_size != 0:
        try:
            from system.pnl_math import floating_pnl_gbp_from_prices

            epic = str(out.get("epic") or out.get("market") or "").strip()
            spread_px = 0.0
            try:
                spread_px = float(out.get("spread") or 0)
            except (TypeError, ValueError):
                spread_px = 0.0
            scaled = floating_pnl_gbp_from_prices(
                epic=epic,
                side=side,
                entry=entry,
                mark=latest,
                size=abs(signed_size),
                spread_price=spread_px,
            )
            if scaled is not None:
                floating_pnl = scaled
            else:
                floating_pnl = (entry - latest) * signed_size
        except Exception:
            floating_pnl = (entry - latest) * signed_size

    if floating_pnl is not None:
        out["floating_pnl_gbp"] = round(float(floating_pnl), 2)
        if out.get("profitAndLoss") is None:
            out["profitAndLoss"] = out["floating_pnl_gbp"]
        if out.get("pnl_gbp") is None:
            out["pnl_gbp"] = out["floating_pnl_gbp"]

    return out


def _normalize_positions_for_ui(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize IG position rows — signed size → side badge + floating P&L."""
    out = dict(payload)
    pmap = out.get("position_map")
    if isinstance(pmap, dict) and pmap:
        normalized_map = {
            deal_id: _normalize_position_row_for_ui(row)
            for deal_id, row in pmap.items()
            if isinstance(row, dict)
        }
        out["position_map"] = normalized_map
    positions = out.get("positions")
    if isinstance(positions, list):
        out["positions"] = [
            _normalize_position_row_for_ui(row) for row in positions if isinstance(row, dict)
        ]
    return out


def _parse_closed_at_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _closure_reason_label(row: dict[str, Any]) -> str:
    for key in ("closure_reason", "exit_reason", "close_reason"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).replace("_", " ").strip().upper()
    notes = str(row.get("notes") or "").strip()
    if notes:
        return notes[:64]
    result = str(row.get("result") or "").strip().upper()
    if result and result not in ("PENDING", "UNCONFIRMED", "OPEN"):
        return result
    source = str(row.get("source") or "").strip()
    if source:
        return source.replace("_", " ").upper()
    return "CLOSE"


def _normalize_closed_trade_row_for_ui(row: dict[str, Any]) -> dict[str, Any]:
    """Map broker / learning-store closed trade row to Flight Deck history grid."""
    out = _normalize_position_row_for_ui(row)
    epic = str(out.get("epic") or out.get("market") or "").strip()
    out["epic"] = epic

    entry = out.get("entry_price") or out.get("entry")
    exit_px = out.get("exit_price") or out.get("exit")
    out["entry"] = entry
    out["exit"] = exit_px
    out["entry_price"] = entry
    out["exit_price"] = exit_px

    pnl = out.get("realized_pnl_gbp")
    if pnl is None:
        pnl = out.get("pnl_gbp")
    if pnl is None:
        pnl = out.get("ig_pnl_currency")
    if pnl is None:
        pnl = out.get("pnl_points")
    if pnl is None:
        pnl = out.get("pnl")
    if pnl is not None:
        try:
            realized = round(float(pnl), 2)
            out["realized_pnl_gbp"] = realized
            out["pnl_gbp"] = realized
            if out.get("profitAndLoss") is None:
                out["profitAndLoss"] = realized
        except (TypeError, ValueError):
            pass

    out["closure_reason"] = _closure_reason_label(out)
    return out


def _closed_trades_last_24h(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        closed_at = _parse_closed_at_ts(row.get("closed_at") or row.get("time"))
        if closed_at is not None and closed_at < cutoff:
            continue
        out.append(_normalize_closed_trade_row_for_ui(row))
    out.sort(
        key=lambda r: str(r.get("closed_at") or r.get("time") or ""),
        reverse=True,
    )
    return out


def _fetch_local_closed_trades_source() -> list[dict[str, Any]]:
    try:
        from data.learning_store import LearningStore
        from system.closed_trades_display import deduplicate_ig_imports, is_excluded_display_row
        from system.config_loader import ConfigLoader
        from system.paths import config_dir

        from system.config_loader import load_active_config

        cfg = load_active_config(validate=False)
        store = LearningStore(str(cfg.learning_db))
        rows = store.recent_agent_closed_trades(limit=200)
        filtered = [r for r in rows if not is_excluded_display_row(r)]
        return deduplicate_ig_imports(filtered)
    except Exception:
        pass
    try:
        from api.dashboard_data import get_closed_trades

        return get_closed_trades(limit=100)
    except Exception:
        return []


def _parse_ig_transactions_for_ui(
    txns: list[dict[str, Any]],
    rest: Any,
    *,
    hours: float = 24.0,
) -> list[dict[str, Any]]:
    """Normalise raw IG /history/transactions rows for Flight Deck closed-trade grid."""
    from system.ig_transactions import (
        build_activity_time_lookup,
        filter_rows_last_hours,
        ig_date_range_dd_mm_yyyy,
        parse_ig_transaction_row,
    )

    activity_times: dict[str, str] = {}
    if hasattr(rest, "fetch_account_activity"):
        try:
            days_back = max(1, int((max(1.0, float(hours)) + 23.0) // 24.0))
            start, end = ig_date_range_dd_mm_yyyy(days_back=days_back)
            activities = rest.fetch_account_activity(start, end)
            activity_times = build_activity_time_lookup(activities)
        except Exception:
            activity_times = {}

    rows: list[dict[str, Any]] = []
    for txn in txns:
        if not isinstance(txn, dict):
            continue
        row = parse_ig_transaction_row(txn, activity_times=activity_times)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    return filter_rows_last_hours(rows, hours)


def _fetch_broker_closed_rows_from_ig(*, hours: float = 24.0) -> list[dict[str, Any]]:
    """Pull closed deals from IG REST — broker ledger is source of truth."""
    try:
        from runtime.ig_transaction_sync import get_transaction_sync_instance

        sync = get_transaction_sync_instance()
        if sync is not None and str(sync.last_sync_at or "").strip():
            cached = sync.get_display_rows(limit=250, hours=hours)
            if cached:
                return cached
    except Exception:
        pass

    try:
        from system.credentials_loader import load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        creds = load_credentials()
        rest = ensure_shared_authenticated(creds)
        if not hasattr(rest, "fetch_transaction_history"):
            from system.ig_transactions import ig_date_range_dd_mm_yyyy

            days_back = max(1, int((max(1.0, float(hours)) + 23.0) // 24.0))
            start, end = ig_date_range_dd_mm_yyyy(days_back=days_back)
            txns = rest.fetch_transactions(start, end, transaction_type="ALL_DEAL", page_size=500)
        else:
            txns = rest.fetch_transaction_history(hours=hours)
        return _parse_ig_transactions_for_ui(list(txns or []), rest, hours=hours)
    except Exception as exc:
        log_engine(
            f"Flight Deck broker closed-trades fetch failed: {type(exc).__name__}: {exc}"
        )
        return []


def _refresh_broker_closed_trades_cache(
    *,
    hours: float = 24.0,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Hydrate in-memory broker ledger cache (TTL-gated to protect REST budget)."""
    global _broker_closed_rows, _broker_closed_fetched_at, _broker_closed_bootstrapped
    now = time.time()
    with _broker_closed_lock:
        if (
            not force
            and _broker_closed_bootstrapped
            and (now - _broker_closed_fetched_at) < _BROKER_CLOSED_CACHE_TTL_SEC
        ):
            return list(_broker_closed_rows)

    rows = _fetch_broker_closed_rows_from_ig(hours=hours)
    with _broker_closed_lock:
        _broker_closed_rows = list(rows)
        _broker_closed_fetched_at = now
        _broker_closed_bootstrapped = True
    if rows:
        log_engine(
            f"Flight Deck: IG broker closed-trades hydrated ({len(rows)} rows / {hours:.0f}h)"
        )
    return list(rows)


def prepare_closed_trades_broker_cache_on_startup() -> None:
    """Non-blocking prefetch — mirrors triage ledger startup hydration."""

    def _work() -> None:
        try:
            _refresh_broker_closed_trades_cache(hours=24.0, force=True)
        except Exception as exc:
            log_engine(
                f"Flight Deck broker history prefetch failed: {type(exc).__name__}: {exc}"
            )

    threading.Thread(
        target=_work,
        name="cockpit-broker-history-prefetch",
        daemon=True,
    ).start()


def _get_broker_closed_trades_for_ui(*, hours: float = 24.0) -> list[dict[str, Any]]:
    """Reuse broker ledger cache for 10 minutes — avoids refresh spam on client reload."""
    return _refresh_broker_closed_trades_cache(hours=hours, force=False)


def _merge_closed_trades_for_ui(
    broker_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """IG broker ledger wins; local session rows fill gaps until history API catches up."""
    from system.closed_trades_merger import merge_closed_trades

    merged, _label = merge_closed_trades(broker_rows, local_rows, limit=250)
    return merged


def _attach_closed_trades_for_ui(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach 24h closed transaction history for Flight Deck Card D."""
    out = dict(payload)
    incoming: list[dict[str, Any]] = []
    for key in ("closed_trades", "closed_executions", "transaction_history"):
        raw = out.get(key)
        if isinstance(raw, list) and raw:
            incoming.extend(r for r in raw if isinstance(r, dict))

    broker_rows = _get_broker_closed_trades_for_ui(hours=24.0)
    local_rows = incoming if incoming else _fetch_local_closed_trades_source()
    merged = _merge_closed_trades_for_ui(broker_rows, local_rows)
    history = _closed_trades_last_24h(merged)
    out["closed_trades_24h"] = history
    out["closed_trades"] = history
    out["closed_trades_broker_count"] = len(broker_rows)
    out["closed_trades_local_count"] = len(local_rows)
    return out


def broadcast_system_hot_reload(*, source: str = "supervisor") -> None:
    """Queue a SYSTEM_HOT_RELOAD frame for all /ws/telemetry clients."""
    global _hot_reload_pending
    with _hot_reload_lock:
        _hot_reload_pending = True
    log_engine(f"Flight Deck: SYSTEM_HOT_RELOAD queued (source={source})")


def _pop_hot_reload_frame() -> dict[str, Any] | None:
    global _hot_reload_pending
    with _hot_reload_lock:
        if not _hot_reload_pending:
            return None
        _hot_reload_pending = False
    return {
        "type": "SYSTEM_HOT_RELOAD",
        "ts": time.time(),
        "source": "supervisor",
    }


def _read_engine_log_batch(*, max_lines: int = 24) -> list[str]:
    """Tail new lines from engine.log for the avionics HUD."""
    global _log_tail_offset
    from system.paths import logs_dir

    path = logs_dir() / "engine.log"
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with _log_tail_lock:
            if _log_tail_offset > size:
                _log_tail_offset = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(_log_tail_offset)
                chunk = fh.read()
                _log_tail_offset = fh.tell()
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines
    except OSError:
        return []


def create_cockpit_app() -> Any:
    web_root = cockpit_web_root()
    app = FastAPI(title="IG Agent Flight Deck", version="29.1", docs_url=None, redoc_url=None)

    if web_root.is_dir():
        app.mount("/static", StaticFiles(directory=str(web_root)), name="cockpit-static")

    @app.get("/")
    async def cockpit_index():
        index = web_root / "index.html"
        return FileResponse(
            str(index),
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    @app.get("/api/health")
    async def cockpit_health():
        return JSONResponse({"ok": True, "service": "flight_deck_web"})

    @app.get("/api/cockpit-controls")
    async def cockpit_controls():
        """Operator lock state for Flight Deck toggles (shadow engine, etc.)."""
        return JSONResponse(sanitize_for_ws_json(_build_cockpit_controls_payload()))

    @app.post("/api/shadow/toggle")
    async def cockpit_shadow_toggle():
        """Toggle IG_AGENT_MODE SHADOW/LIVE when cockpit controls are unlocked."""
        controls = _build_cockpit_controls_payload()
        if controls.get("disabled"):
            return JSONResponse(
                {"ok": False, "error": "controls_locked", "controls": controls},
                status_code=403,
            )
        import os

        active = os.environ.get("IG_AGENT_MODE", "").strip().upper() == "SHADOW"
        new_mode = "LIVE" if active else "SHADOW"
        os.environ["IG_AGENT_MODE"] = new_mode
        log_engine(f"Flight Deck: shadow trading engine -> {new_mode}")
        return JSONResponse(
            sanitize_for_ws_json(
                {
                    "ok": True,
                    "mode": new_mode,
                    "controls": _build_cockpit_controls_payload(),
                }
            )
        )

    @app.get("/api/drawdown-status")
    async def cockpit_drawdown_status():
        """Ops verification — Superjet guard + decimal monitor snapshot."""
        try:
            from system.drawdown_monitor import operational_status, snapshot_for_telemetry
            from system.superjet_drawdown_guard import is_frozen, telemetry_snapshot

            guard = telemetry_snapshot()
            guard["monitor"] = snapshot_for_telemetry()
            guard["monitor_operational_status"] = operational_status()
            guard["lockout_clear"] = (
                not guard.get("frozen")
                and not guard.get("breached")
                and operational_status() in ("NOMINAL", "STANDBY")
            )
            return JSONResponse(sanitize_for_ws_json(guard))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/emergency")
    async def cockpit_emergency():
        try:
            from cockpit.telemetry_bridge import get_command_queue

            get_command_queue().put_nowait("EMERGENCY_FLATTEN")
            log_engine("Flight Deck web: EMERGENCY_FLATTEN queued")
            return JSONResponse({"ok": True, "status": "queued"})
        except queue.Full:
            return JSONResponse({"ok": False, "error": "command queue full"}, status_code=503)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                hot_reload = _pop_hot_reload_frame()
                if hot_reload is not None:
                    await _ws_send_json_frame(ws, hot_reload, channel="telemetry")
                    continue
                payload = get_latest_telemetry()
                if not payload:
                    payload = {"ts": time.time(), "gates": {}, "epics": {}, "spread": {}}
                payload = _enrich_telemetry_for_ui(payload)
                await _ws_send_json_frame(ws, payload, channel="telemetry")
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck WS client error: {type(e).__name__}: {e}")

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                lines = _read_engine_log_batch()
                frame = {
                    "type": "LOG_FRAME",
                    "ts": time.time(),
                    "lines": lines,
                }
                await _ws_send_json_frame(ws, frame, channel="logs")
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck log WS error: {type(e).__name__}: {e}")

    @app.websocket("/ws/triage")
    async def ws_triage(ws: WebSocket) -> None:
        await ws.accept()
        hz = 2.5
        interval = max(0.05, 1.0 / hz)
        try:
            while not _stop.is_set():
                from system.supervisor_history import (
                    consume_triage_boot_reset_flag,
                    get_triage_generation,
                    read_triage_events_for_ui,
                )

                rows = read_triage_events_for_ui(max_lines=120)
                frame = {
                    "type": "TRIAGE_FRAME",
                    "status": "INITIALIZED",
                    "feed_status": "NOMINAL",
                    "ledger_initialized": True,
                    "ts": time.time(),
                    "events": rows,
                    "triage_generation": get_triage_generation(),
                    "reset_client_cache": consume_triage_boot_reset_flag(),
                    "full_sync": True,
                }
                await _ws_send_json_frame(ws, frame, channel="triage")
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log_engine(f"Flight Deck triage WS error: {type(e).__name__}: {e}")

    return app


def start_cockpit_web_server(*, port: int | None = None, hz: float = 2.5) -> bool:
    """Start cockpit FastAPI + WebSocket hub in a daemon thread."""
    bind_port = _cockpit_port() if port is None else int(port)
    global _server, _thread, _loop
    with _lock:
        if _thread is not None and _thread.is_alive():
            log_engine("Flight Deck web server already running")
            return True
        _stop.clear()
        try:
            from system.protective_learning import (
                activate_test_mode_runtime,
                clear_operational_locks_for_test_run,
                ensure_autonomous_engine_on_boot,
            )

            ensure_autonomous_engine_on_boot()
            clear_operational_locks_for_test_run()
            activate_test_mode_runtime()
        except Exception as exc:
            log_engine(
                f"Flight Deck test-mode lock clear skipped: {type(exc).__name__}: {exc}"
            )
        try:
            from system.supervisor_history import prepare_triage_ledger_on_startup

            prepare_triage_ledger_on_startup()
        except Exception as exc:
            log_engine(
                f"Flight Deck triage ledger startup prep failed: {type(exc).__name__}: {exc}"
            )
        try:
            prepare_closed_trades_broker_cache_on_startup()
        except Exception as exc:
            log_engine(
                f"Flight Deck broker history startup prep failed: {type(exc).__name__}: {exc}"
            )

        def _run() -> None:
            global _server, _loop
            import uvicorn

            config = uvicorn.Config(
                create_cockpit_app(),
                host="127.0.0.1",
                port=bind_port,
                log_level="warning",
                access_log=False,
            )
            _server = uvicorn.Server(config)
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            loop = _loop
            try:
                loop.run_until_complete(_server.serve())
            finally:
                if loop is not None and not loop.is_closed():
                    loop.close()

        _thread = threading.Thread(
            target=_run, name="CockpitWebServer", daemon=True
        )
        _thread.start()

    # Wait for bind
    import socket

    deadline = time.time() + 8.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(("127.0.0.1", bind_port))
                log_engine(f"Flight Deck web cockpit live at http://127.0.0.1:{bind_port}/")
                return True
            except OSError:
                time.sleep(0.15)
    log_engine(f"Flight Deck web server failed to bind port {bind_port}")
    return False


def stop_cockpit_web_server() -> None:
    global _server, _thread, _loop
    _stop.set()
    with _lock:
        srv = _server
        loop = _loop
        th = _thread
        _server = None
        _loop = None
        _thread = None
    if srv is not None:
        srv.should_exit = True
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(lambda: None)
        except Exception:
            pass
    if th is not None and th.is_alive():
        th.join(timeout=3.0)
    with _latest_lock:
        _latest_payload.clear()
    with _hot_reload_lock:
        global _hot_reload_pending
        _hot_reload_pending = False


def reset_cockpit_web_for_tests() -> None:
    global _log_tail_offset
    stop_cockpit_web_server()
    _stop.clear()
    with _log_tail_lock:
        _log_tail_offset = 0
