#!/usr/bin/env python3
"""
Live limit torture — paced cadence harness (1 order / 20s) for post-allowance validation.

  PYTHONPATH=src .venv/bin/python3 scripts/inject_live_limit_torture.py

Defaults: 20 paced orders + 20-minute soak. Trailing PUTs use budget_priority bypass.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analytics.torture_ledger import (  # noqa: E402
    build_certification_report,
    record_event,
    upsert_session,
)
from system.config_loader import ConfigLoader  # noqa: E402
from system.share_pacing import SharePacingController  # noqa: E402

TORTURE_EPICS: tuple[str, ...] = (
    "IX.D.DOW.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CFPGOLD.CFP.IP",
)
TRAIL_POINTS = 2.0
SCALP_LIMIT_POINTS = 1.0
ORDER_CADENCE_SEC = 20.0
INDEX_LOT = 0.5
FX_LOT = 1.0


@dataclass
class TortureMetrics:
    orders_ok: int = 0
    orders_processed: int = 0
    orders_fail: int = 0
    connection_drops: int = 0
    trailing_mods: int = 0
    scalp_exits: int = 0
    open_positions: list[dict[str, Any]] = field(default_factory=list)


class PacedTransport:
    """Async transport — trailing PUTs preempt order slots; 20s order cadence."""

    def __init__(self, *, order_interval_sec: float = ORDER_CADENCE_SEC) -> None:
        self.order_interval_sec = float(order_interval_sec)
        self._order_lock = asyncio.Lock()
        self._trail_lock = asyncio.Lock()
        self._last_order_mono = 0.0

    async def run_trailing_put(self, fn: Any) -> Any:
        async with self._trail_lock:
            return await asyncio.to_thread(fn)

    async def run_order(self, fn: Any) -> Any:
        async with self._order_lock:
            now = time.monotonic()
            if self._last_order_mono > 0:
                wait = self.order_interval_sec - (now - self._last_order_mono)
                if wait > 0:
                    await asyncio.sleep(wait)
            result = await asyncio.to_thread(fn)
            self._last_order_mono = time.monotonic()
            return result

    async def pace_sleep(self) -> None:
        """Idle soak tick — maintain 20s spacing between order injections."""
        now = time.monotonic()
        if self._last_order_mono > 0:
            wait = self.order_interval_sec - (now - self._last_order_mono)
            if wait > 0:
                await asyncio.sleep(wait)


def _apply_canary_env() -> None:
    os.environ.setdefault("PROD_MODE", "PRODUCTION")
    os.environ.setdefault("IG_API_PORT", "8080")
    os.environ.setdefault("IG_SHARE_ENGINE", "1")
    os.environ.setdefault("IG_PRODUCTION_EXECUTION", "1")
    os.environ.setdefault("IG_APEX_RUNTIME_MODE", "PRODUCTION")
    os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_live_canary.json")
    os.environ.setdefault("IG_TORTURE_TRAIL_PRIORITY", "1")
    os.environ.setdefault(
        "IG_TRIAGE_DB",
        str(ROOT / "src" / "analytics" / "triage_v31.db"),
    )


def _load_canary_sizing() -> tuple[float, float, float]:
    """Return (index_lot, fx_lot, order_cadence_sec) from canary config."""
    path = ROOT / "config" / "config_v31_live_canary.json"
    try:
        raw = ConfigLoader(path).load_config(validate=False).as_dict()
        exe = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
        index_lot = float(
            exe.get("max_deal_size_index")
            or raw.get("max_deal_size")
            or INDEX_LOT
        )
        fx_lot = float(
            exe.get("max_deal_size_fx")
            or raw.get("max_deal_size_fx")
            or FX_LOT
        )
        cadence = float(exe.get("order_cadence_sec") or ORDER_CADENCE_SEC)
        env_cadence = os.environ.get("IG_TORTURE_ORDER_CADENCE_SEC", "").strip()
        if env_cadence:
            cadence = float(env_cadence)
        return index_lot, fx_lot, cadence
    except Exception:
        return INDEX_LOT, FX_LOT, ORDER_CADENCE_SEC


def _epic_micro_lot(epic: str, *, index_lot: float, fx_lot: float) -> float:
    """Exchange-minimum deal size — 0.5 indices/commodities, 1.0 FX."""
    key = str(epic or "").upper()
    if "EURUSD" in key or ("USD" in key and "CFD" in key and "CFP" not in key):
        return fx_lot
    return index_lot


def _deal_ref(tag: str) -> str:
    seed = f"{tag}-{secrets.token_hex(12)}-{os.getpid()}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _post_fulfill_sync(api_base: str, payload: dict, *, timeout: float = 60.0) -> tuple[dict, float]:
    url = f"{api_base.rstrip('/')}/api/v31/orders/fulfill"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        code = int(resp.status if hasattr(resp, "status") else resp.getcode())
        if code == 202 and isinstance(body, dict):
            body.setdefault("ok", True)
            body.setdefault("status", "ACCEPTED")
            body.setdefault("async", True)
        return body, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = {"error": raw, "http_status": exc.code}
        return {"ok": False, "status": "HTTP_ERROR", "detail": detail}, (time.perf_counter() - t0) * 1000.0
    except Exception as exc:
        return {"ok": False, "status": "CONNECTION_DROP", "error": str(exc)}, (time.perf_counter() - t0) * 1000.0


def _record_order_result(
    *,
    session_id: str,
    epic: str,
    direction: str,
    receipt: dict,
    rtt_ms: float,
    metrics: TortureMetrics,
) -> None:
    status = str(receipt.get("status") or "").upper()
    if receipt.get("dealReference") or receipt.get("dealId") or receipt.get("place"):
        metrics.orders_processed += 1
    if (
        receipt.get("ok")
        or status in ("CONFIRMED", "EXECUTED", "ACCEPTED")
        or receipt.get("async")
    ):
        metrics.orders_ok += 1
        deal_id = receipt.get("dealId") or receipt.get("deal_id")
        print(
            f"TORTURE order_ok epic={epic} {direction} status={status} "
            f"dealId={deal_id or '—'} rtt_ms={rtt_ms:.0f}"
            f"{' async' if receipt.get('async') else ''}"
        )
        record_event(
            session_id=session_id,
            event_type="order_ok",
            epic=epic,
            deal_id=str(deal_id) if deal_id else None,
            direction=direction,
            detail=receipt,
            rtt_ms=rtt_ms,
        )
        if deal_id:
            metrics.open_positions.append(
                {
                    "deal_id": str(deal_id),
                    "epic": epic,
                    "direction": direction,
                }
            )
    elif status == "CONNECTION_DROP" or receipt.get("status") == "CONNECTION_DROP":
        metrics.connection_drops += 1
        metrics.orders_fail += 1
        record_event(
            session_id=session_id,
            event_type="connection_drop",
            epic=epic,
            direction=direction,
            detail=receipt,
            rtt_ms=rtt_ms,
        )
    else:
        metrics.orders_fail += 1
        print(
            f"TORTURE order_fail epic={epic} {direction} status={status} "
            f"rtt_ms={rtt_ms:.0f} detail={str(receipt.get('detail') or receipt.get('error') or receipt.get('reason') or '')[:120]}"
        )
        record_event(
            session_id=session_id,
            event_type="order_fail",
            epic=epic,
            direction=direction,
            detail=receipt,
            rtt_ms=rtt_ms,
        )


async def _inject_order(
    *,
    api_base: str,
    session_id: str,
    epic: str,
    direction: str,
    seq: int,
    metrics: TortureMetrics,
    share: SharePacingController,
    transport: PacedTransport,
    index_lot: float,
    fx_lot: float,
) -> None:
    size = _epic_micro_lot(epic, index_lot=index_lot, fx_lot=fx_lot)
    place_stop = max(10.0, TRAIL_POINTS)
    place_limit = max(SCALP_LIMIT_POINTS, place_stop * 0.5)
    payload = {
        "signal": "BREAKOUT",
        "epic": epic,
        "direction": direction,
        "size": size,
        "dealReference": _deal_ref(f"torture-{seq}"),
        "trailing_stop_distance": place_stop,
        "scalp_limit": place_limit,
        "setup_key": f"TORTURE|{direction}|{epic}",
        "confidence": 90.0,
    }

    def _fire() -> tuple[dict, float]:
        return _post_fulfill_sync(api_base, payload)

    receipt, rtt_ms = await transport.run_order(_fire)
    prof = share.observe_rtt(rtt_ms)
    if prof.shifts:
        record_event(
            session_id=session_id,
            event_type="share_pacing_shift",
            detail=prof.shifts[-1],
            rtt_ms=rtt_ms,
        )
    _record_order_result(
        session_id=session_id,
        epic=epic,
        direction=direction,
        receipt=receipt,
        rtt_ms=rtt_ms,
        metrics=metrics,
    )


def _build_burst_matrix() -> list[tuple[str, str]]:
    """20 paced orders — bidirectional hedge per epic (5 rounds across 4 markets)."""
    jobs: list[tuple[str, str]] = []
    for i in range(5):
        for epic in TORTURE_EPICS:
            jobs.append((epic, "BUY" if i % 2 == 0 else "SELL"))
    return jobs[:20]


def _apply_trailing_put(
    rest: Any,
    *,
    deal_id: str,
    epic: str,
    direction: str,
    level: float,
    step: float,
    index_lot: float,
    fx_lot: float,
) -> tuple[bool, str, dict[str, Any] | None]:
    from execution.atomic_gateway import order_dispatch_lane
    from execution.live_broker_order_router import (
        apply_step_trail_put,
        compute_step_trail_update,
    )

    update = compute_step_trail_update(
        rest,
        epic=epic,
        direction=direction,
        deal_id=deal_id,
        entry_level=level,
        step_points=TRAIL_POINTS,
        scalp_limit_points=SCALP_LIMIT_POINTS,
        iteration=int(step),
        market_price=level,
    )

    def _put() -> dict[str, Any]:
        with order_dispatch_lane():
            return apply_step_trail_put(rest, update, budget_priority=True)

    try:
        result = _put()
        return True, deal_id, {
            "stop_level": update.stop_level,
            "limit_level": update.limit_level,
            "min_stop_points": update.min_stop_points,
            "floored_step": update.floored_step,
            "raw": result,
        }
    except Exception as exc:
        return False, deal_id, {"error": str(exc)}


async def _trailing_stress_loop(
    *,
    session_id: str,
    metrics: TortureMetrics,
    share: SharePacingController,
    transport: PacedTransport,
    stop_event: asyncio.Event,
    index_lot: float,
    fx_lot: float,
) -> None:
    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    creds = try_load_credentials()
    if not creds.ok or creds.credentials is None:
        return
    rest = ensure_shared_authenticated(creds.credentials)
    step = 0.0
    refresh_every = 3
    cycle = 0

    while not stop_event.is_set():
        await asyncio.sleep(max(5.0, transport.order_interval_sec / 4.0))
        t0 = time.perf_counter()
        try:
            targets = list(metrics.open_positions)
            if not targets or cycle % refresh_every == 0:
                def _refresh() -> list[dict[str, Any]]:
                    rest.login()
                    from execution.atomic_gateway import order_dispatch_lane

                    with order_dispatch_lane():
                        return rest.open_positions() or []

                rows = await transport.run_trailing_put(_refresh)
                targets = []
                for item in rows:
                    pos = item.get("position") or {}
                    mkt = item.get("market") or {}
                    deal_id = str(pos.get("dealId") or "")
                    if deal_id:
                        targets.append(
                            {
                                "deal_id": deal_id,
                                "epic": str(mkt.get("epic") or ""),
                                "direction": str(pos.get("direction") or "BUY").upper(),
                                "level": float(pos.get("level") or pos.get("openLevel") or 0),
                                "upl": float(pos.get("upl") or pos.get("profit") or 0),
                                "size": float(pos.get("size") or 0),
                            }
                        )

            for pos in targets:
                deal_id = str(pos.get("deal_id") or "")
                epic = str(pos.get("epic") or "")
                direction = str(pos.get("direction") or "BUY").upper()
                level = float(pos.get("level") or 0)
                if not deal_id or level <= 0:
                    continue
                step += TRAIL_POINTS * 0.1

                def _do_put() -> tuple[bool, str, dict[str, Any] | None]:
                    rest.login()
                    return _apply_trailing_put(
                        rest,
                        deal_id=deal_id,
                        epic=epic,
                        direction=direction,
                        level=level,
                        step=step,
                        index_lot=index_lot,
                        fx_lot=fx_lot,
                    )

                ok, _, detail = await transport.run_trailing_put(_do_put)
                if ok:
                    metrics.trailing_mods += 1
                    record_event(
                        session_id=session_id,
                        event_type="trailing_mod",
                        epic=epic,
                        deal_id=deal_id,
                        direction=direction,
                        detail=detail,
                    )
                else:
                    record_event(
                        session_id=session_id,
                        event_type="trailing_mod_fail",
                        epic=epic,
                        deal_id=deal_id,
                        detail=detail,
                    )

                upl = float(pos.get("upl") or 0)
                if upl > 0 and abs(upl) >= SCALP_LIMIT_POINTS * 0.5:
                    def _close() -> None:
                        from execution.atomic_gateway import order_dispatch_lane

                        rest.login()
                        close_dir = "SELL" if direction == "BUY" else "BUY"
                        size = float(pos.get("size") or _epic_micro_lot(epic, index_lot=index_lot, fx_lot=fx_lot))
                        with order_dispatch_lane():
                            rest.close_position(
                                deal_id,
                                direction=close_dir,
                                size=size,
                                epic=epic,
                                verify=False,
                            )

                    try:
                        await transport.run_trailing_put(_close)
                        metrics.scalp_exits += 1
                        record_event(
                            session_id=session_id,
                            event_type="scalp_exit",
                            epic=epic,
                            deal_id=deal_id,
                            direction=direction,
                            detail={"upl": upl},
                        )
                    except Exception as exc:
                        record_event(
                            session_id=session_id,
                            event_type="scalp_exit_fail",
                            epic=epic,
                            deal_id=deal_id,
                            detail=str(exc),
                        )
            cycle += 1
        except Exception as exc:
            metrics.connection_drops += 1
            record_event(
                session_id=session_id,
                event_type="connection_drop",
                detail=str(exc),
            )
        share.observe_rtt((time.perf_counter() - t0) * 1000.0)


async def run_torture(*, soak_sec: float, api_base: str) -> dict[str, Any]:
    _apply_canary_env()
    index_lot, fx_lot, cadence = _load_canary_sizing()
    session_id = f"torture-{int(time.time())}"
    upsert_session(session_id)
    share = SharePacingController(min_pacing_ms=cadence * 1000.0, max_pacing_ms=cadence * 1000.0)
    share.profile.pacing_ms = cadence * 1000.0
    transport = PacedTransport(order_interval_sec=cadence)
    metrics = TortureMetrics()

    burst_jobs = _build_burst_matrix()
    print(
        f"TORTURE: paced cadence — 1 order / {cadence:.0f}s "
        f"(index={index_lot} fx={fx_lot}) × {len(burst_jobs)} injections"
    )
    for i, (epic, direction) in enumerate(burst_jobs):
        await _inject_order(
            api_base=api_base,
            session_id=session_id,
            epic=epic,
            direction=direction,
            seq=i,
            metrics=metrics,
            share=share,
            transport=transport,
            index_lot=index_lot,
            fx_lot=fx_lot,
        )

    stop_event = asyncio.Event()
    trailing_task = asyncio.create_task(
        _trailing_stress_loop(
            session_id=session_id,
            metrics=metrics,
            share=share,
            transport=transport,
            stop_event=stop_event,
            index_lot=index_lot,
            fx_lot=fx_lot,
        )
    )

    print(f"TORTURE: {soak_sec:.0f}s soak @ {cadence:.0f}s/order — trailing PUT priority armed")
    soak_end = time.time() + soak_sec
    seq = 10_000
    while time.time() < soak_end:
        epic = TORTURE_EPICS[seq % len(TORTURE_EPICS)]
        direction = "BUY" if seq % 2 == 0 else "SELL"
        await _inject_order(
            api_base=api_base,
            session_id=session_id,
            epic=epic,
            direction=direction,
            seq=seq,
            metrics=metrics,
            share=share,
            transport=transport,
            index_lot=index_lot,
            fx_lot=fx_lot,
        )
        seq += 1

    stop_event.set()
    await trailing_task

    pacing_vals = [float(s.get("pacing_ms", 0)) for s in share.profile.shifts if s.get("pacing_ms")]
    upsert_session(
        session_id,
        ended_at=time.time(),
        orders_ok=metrics.orders_ok,
        orders_fail=metrics.orders_fail,
        connection_drops=metrics.connection_drops,
        trailing_mods=metrics.trailing_mods,
        scalp_exits=metrics.scalp_exits,
        share_min_pacing_ms=min(pacing_vals) if pacing_vals else cadence * 1000.0,
        share_max_pacing_ms=max(pacing_vals) if pacing_vals else cadence * 1000.0,
    )
    report = build_certification_report(session_id)
    report["concurrency_capacity"]["orders_processed"] = metrics.orders_processed
    report["paced_cadence_sec"] = cadence
    report["deal_sizes"] = {"index_lot": index_lot, "fx_lot": fx_lot}
    report["live_metrics"] = {
        "orders_ok": metrics.orders_ok,
        "orders_processed": metrics.orders_processed,
        "orders_fail": metrics.orders_fail,
        "connection_drops": metrics.connection_drops,
        "trailing_mods": metrics.trailing_mods,
        "scalp_exits": metrics.scalp_exits,
        "share_workers_final": share.profile.workers,
        "share_pacing_ms_final": share.profile.pacing_ms,
        "trailing_put_priority": True,
    }
    upsert_session(session_id, report_json=json.dumps(report, default=str))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Live limit torture — paced cadence harness")
    parser.add_argument("--soak-sec", type=float, default=1200.0, help="Soak duration (default 20 min)")
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--cadence-sec",
        type=float,
        default=0.0,
        help="Override order cadence (default: config execution.order_cadence_sec or 20)",
    )
    args = parser.parse_args()

    if args.cadence_sec > 0:
        os.environ["IG_TORTURE_ORDER_CADENCE_SEC"] = str(args.cadence_sec)

    report = asyncio.run(run_torture(soak_sec=args.soak_sec, api_base=args.api_base))
    print("\n=== ABSOLUTE LOGIC CERTIFICATION LEDGER ===")
    print(json.dumps(report, indent=2, default=str))
    cap = report.get("concurrency_capacity", {})
    ok = int(cap.get("orders_ok", 0))
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
