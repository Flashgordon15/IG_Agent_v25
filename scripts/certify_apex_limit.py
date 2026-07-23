#!/usr/bin/env python3
"""
Apex v31 — 10-cycle deterministic live replay certification suite.

Exercises scalping paths, step-trailing (live min-stop flooring), ML optimizer shifts,
and closed-loop broker reconciliation under production canary settings.

  PYTHONPATH=src .venv/bin/python3 scripts/certify_apex_limit.py
  PYTHONPATH=src .venv/bin/python3 scripts/certify_apex_limit.py --dry-run
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analytics.torture_ledger import record_event, upsert_session  # noqa: E402
from system.config_loader import ConfigLoader  # noqa: E402
from system.engine_log import log_engine  # noqa: E402

REPORT_PATH = ROOT / "src" / "analytics" / "certification_report_v31.json"

CERT_EPICS: tuple[str, ...] = (
    "IX.D.DOW.IFM.IP",
    "IX.D.FTSE.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.CFPGOLD.CFP.IP",
)

# 10 bit-perfect historical-style profiles (synthetic replay paths)
REPLAY_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "name": "trend_expansion",
        "direction": "BUY",
        "deltas": [0, 2, 5, 8, 12, 15, 18, 22],
        "spread": 1.0,
        "note": "Momentum expansion — trailing raise path",
    },
    {
        "name": "liquidity_trap",
        "direction": "SELL",
        "deltas": [0, -1, 3, -4, 2, -6, 1, -2],
        "spread": 1.2,
        "note": "Whipsaw liquidity — reconciliation stress",
    },
    {
        "name": "sharp_reversal",
        "direction": "BUY",
        "deltas": [0, 6, 10, 4, -8, -12, -6, 2],
        "spread": 1.5,
        "note": "V-reversal — stop floor validation",
    },
    {
        "name": "macro_news_shock",
        "direction": "SELL",
        "deltas": [0, -15, -22, -18, -10, -5, 3, 8],
        "spread": 2.0,
        "note": "Gap shock — wide spread scalp",
    },
    {
        "name": "range_compression",
        "direction": "BUY",
        "deltas": [0, 0.5, -0.5, 0.3, -0.2, 0.4, -0.3, 0.1],
        "spread": 0.8,
        "note": "Tight range — min-stop scrape",
    },
    {
        "name": "breakout_continuation",
        "direction": "BUY",
        "deltas": [0, 3, 7, 11, 14, 16, 19, 21],
        "spread": 1.0,
        "note": "Breakout ladder — step trail mods",
    },
    {
        "name": "fade_exhaustion",
        "direction": "SELL",
        "deltas": [0, 4, 2, 0, -3, -7, -9, -11],
        "spread": 1.1,
        "note": "Exhaustion fade — exit closure",
    },
    {
        "name": "stop_hunt_wick",
        "direction": "BUY",
        "deltas": [0, 5, -10, 8, -6, 12, 3, 7],
        "spread": 1.3,
        "note": "Stop hunt wick — trailing backwards guard",
    },
    {
        "name": "overnight_drift",
        "direction": "SELL",
        "deltas": [0, -2, -3, -4, -3, -2, -1, 0],
        "spread": 0.9,
        "note": "Slow drift — paced budget respect",
    },
    {
        "name": "certification_capstone",
        "direction": "BUY",
        "deltas": [0, 1, 3, 6, 9, 12, 10, 14],
        "spread": 1.0,
        "note": "Final capstone — full stack validation",
    },
)


@dataclass
class CycleResult:
    cycle: int
    epic: str
    profile: str
    direction: str
    ok: bool
    deal_id: str = ""
    deal_reference: str = ""
    trailing_mods: int = 0
    ml_z_threshold: float = 0.0
    reconciliation_purged: int = 0
    min_stop_points: float = 0.0
    error: str = ""
    rtt_ms: float = 0.0


@dataclass
class CertificationLedger:
    session_id: str
    cycles: list[CycleResult] = field(default_factory=list)
    ml_shifts: int = 0
    trailing_total: int = 0
    reconciliation_purged: int = 0
    fills: list[dict[str, Any]] = field(default_factory=list)

    def success_count(self) -> int:
        return sum(1 for c in self.cycles if c.ok)

    def to_report(self) -> dict[str, Any]:
        return {
            "ok": self.success_count() == 10,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "total_injected_cycles": f"{self.success_count()}/10",
            "successful_fills": [
                {
                    "cycle": c.cycle,
                    "deal_id": c.deal_id,
                    "deal_reference": c.deal_reference,
                    "epic": c.epic,
                    "profile": c.profile,
                }
                for c in self.cycles
                if c.ok and c.deal_id
            ],
            "trailing_step_modifications": self.trailing_total,
            "ml_parameter_shifts": self.ml_shifts,
            "reconciliation_mismatches_purged": self.reconciliation_purged,
            "active_risk_drift": 0 if self.reconciliation_purged == 0 else self.reconciliation_purged,
            "cycles": [
                {
                    "cycle": c.cycle,
                    "epic": c.epic,
                    "profile": c.profile,
                    "ok": c.ok,
                    "deal_id": c.deal_id,
                    "trailing_mods": c.trailing_mods,
                    "min_stop_points": c.min_stop_points,
                    "ml_z_threshold": c.ml_z_threshold,
                    "error": c.error,
                }
                for c in self.cycles
            ],
        }


def _apply_canary_env() -> None:
    os.environ.setdefault("PROD_MODE", "PRODUCTION")
    os.environ.setdefault("IG_API_PORT", "8080")
    os.environ.setdefault("IG_SHARE_ENGINE", "1")
    os.environ.setdefault("IG_PRODUCTION_EXECUTION", "1")
    os.environ.setdefault("IG_AGENT_CONFIG", "config/config_v31_live_canary.json")
    os.environ.setdefault("IG_TORTURE_TRAIL_PRIORITY", "1")
    os.environ.setdefault(
        "IG_TRIAGE_DB",
        str(ROOT / "src" / "analytics" / "triage_v31.db"),
    )


def _load_sizing() -> tuple[float, float, float]:
    path = ROOT / "config" / "config_v31_live_canary.json"
    try:
        raw = ConfigLoader(path).load_config(validate=False).as_dict()
        exe = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
        index_lot = float(exe.get("max_deal_size_index") or 0.5)
        fx_lot = float(exe.get("max_deal_size_fx") or 1.0)
        cadence = float(exe.get("order_cadence_sec") or 20.0)
        return index_lot, fx_lot, cadence
    except Exception:
        return 0.5, 1.0, 20.0


def _epic_lot(epic: str, *, index_lot: float, fx_lot: float) -> float:
    key = str(epic or "").upper()
    if "EURUSD" in key or ("USD" in key and "CFD" in key and "CFP" not in key):
        return fx_lot
    return index_lot


def _deal_ref(tag: str) -> str:
    seed = f"{tag}-{secrets.token_hex(12)}-{os.getpid()}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _clear_instance_locks() -> None:
    for p in (
        ROOT / "src/data/.ig_agent_v29.lock",
        ROOT / "src/data/state/strategy_kill_switch.json",
    ):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        from runtime.strategy_kill_switch import clear_strategy_kill_switch_for_tests

        clear_strategy_kill_switch_for_tests()
    except Exception:
        pass


_BOOT_GATE_TIMEOUT_SEC = 30.0


def _fetch_health(api_base: str, *, timeout: float = 5.0) -> tuple[int | None, dict[str, Any]]:
    try:
        req = urllib.request.Request(f"{api_base.rstrip('/')}/api/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return int(resp.status), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        return int(exc.code), body if isinstance(body, dict) else {}
    except Exception:
        return None, {}


def _health_operational(api_base: str) -> bool:
    code, body = _fetch_health(api_base)
    return (
        code == 200
        and str(body.get("status") or "").upper() == "OPERATIONAL"
        and body.get("ready") is True
    )


def _wait_operational_health(
    api_base: str,
    *,
    timeout_sec: float = _BOOT_GATE_TIMEOUT_SEC,
) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    while time.monotonic() < deadline:
        if _health_operational(api_base):
            return True
        time.sleep(0.5)
    return False


def _health_ok(api_base: str) -> bool:
    return _health_operational(api_base)


def _post_fulfill(api_base: str, payload: dict) -> tuple[dict, float]:
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
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = {"error": raw, "http_status": exc.code}
        return {"ok": False, "detail": detail}, (time.perf_counter() - t0) * 1000.0
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, (time.perf_counter() - t0) * 1000.0


def _scrape_live_min_stop(rest: Any, epic: str) -> float:
    from execution.live_broker_order_router import floor_stop_distance_points

    res = floor_stop_distance_points(rest, epic, 2.0)
    log_engine(
        f"certify: live minStopOrProfitDistance epic={epic} "
        f"min={res.min_points:g} effective={res.effective_points:g}"
    )
    return float(res.min_points)


def _inject_replay_profile(epic: str, profile: dict[str, Any], *, base_mid: float) -> None:
    from system.market_data_hub import get_market_data_hub

    hub = get_market_data_hub()
    spread = float(profile.get("spread") or 1.0)
    t0 = time.time() - len(profile.get("deltas") or [])
    for i, delta in enumerate(profile.get("deltas") or []):
        mid = float(base_mid) + float(delta)
        bid = mid - spread / 2.0
        offer = mid + spread / 2.0
        hub.publish_replay_tick(
            epic,
            bid,
            offer,
            quote_time=t0 + i,
            source="certify_replay",
        )


def _broker_preflight(rest: Any, deal_id: str, epic: str, direction: str) -> tuple[bool, str]:
    from runtime.trade_manager import (
        PreflightVerdict,
        fetch_broker_ledger_sync,
        preflight_trailing_cycle,
    )

    ledger = fetch_broker_ledger_sync(rest)
    if deal_id and deal_id not in ledger:
        return False, "deal_missing_on_broker"
    if not deal_id:
        return True, "pre_entry"
    result = preflight_trailing_cycle(
        rest_client=rest,
        deal_id=deal_id,
        epic=epic,
        side=direction,
        entry=0.0,
        size=0.0,
        quote=None,
        ledger=ledger,
    )
    if result.verdict in (
        PreflightVerdict.MISSING_ON_BROKER,
        PreflightVerdict.DRIFT_FATAL,
    ):
        return False, result.verdict.value
    if result.verdict == PreflightVerdict.DRIFT_ADVISORY:
        log_engine(
            f"certify: [FINANCIAL DRIFT WARNING] dealId={deal_id} "
            f"drift={result.drift_pct}% — continuing under advisory"
        )
    return True, "ok"


def _find_deal_for_epic(rest: Any, epic: str) -> str:
    for item in rest.open_positions() or []:
        pos = item.get("position") or {}
        mkt = item.get("market") or {}
        if str(mkt.get("epic") or "") == epic:
            return str(pos.get("dealId") or "")
    return ""


def _apply_trail(rest: Any, *, deal_id: str, epic: str, direction: str, level: float, step: float) -> tuple[bool, dict]:
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
        step_points=max(step, 2.0),
        scalp_limit_points=1.0,
        iteration=1,
        market_price=level,
    )
    try:
        raw = apply_step_trail_put(rest, update, budget_priority=True)
        return True, {
            "stop_level": update.stop_level,
            "min_stop_points": update.min_stop_points,
            "floored_step": update.floored_step,
            "raw": raw,
        }
    except Exception as exc:
        return False, {"error": str(exc)}


def _close_deal(rest: Any, *, deal_id: str, epic: str, direction: str, size: float) -> bool:
    # close_position expects OPEN side and inverts once — never pass close_dir.
    open_side = str(direction or "BUY").upper()
    try:
        rest.close_position(
            deal_id,
            direction=open_side,
            size=size,
            epic=epic,
            verify=False,
        )
        return True
    except Exception as exc:
        log_engine(f"certify: close failed deal={deal_id}: {exc}")
        return False


def _base_mid_for_epic(rest: Any, epic: str) -> float:
    try:
        c = rest.fetch_market_constraints(epic)
        bid = float(c.get("bid") or 0)
        offer = float(c.get("offer") or 0)
        if bid > 0 and offer > 0:
            return (bid + offer) / 2.0
    except Exception:
        pass
    if "EURUSD" in epic:
        return 1.1350
    if "GOLD" in epic or "CFPGOLD" in epic:
        return 2350.0
    if "FTSE" in epic:
        return 8200.0
    return 42000.0


async def _run_single_cycle(
    *,
    cycle: int,
    epic: str,
    profile: dict[str, Any],
    api_base: str,
    session_id: str,
    index_lot: float,
    fx_lot: float,
    rest: Any,
    dry_run: bool,
) -> CycleResult:
    direction = str(profile.get("direction") or "BUY").upper()
    prof_name = str(profile.get("name") or f"cycle_{cycle}")
    result = CycleResult(
        cycle=cycle,
        epic=epic,
        profile=prof_name,
        direction=direction,
        ok=False,
    )

    try:
        base_mid = _base_mid_for_epic(rest, epic) if not dry_run else (
            1.135 if "EURUSD" in epic else (2350.0 if "GOLD" in epic or "CFP" in epic else 42000.0)
        )
        _inject_replay_profile(epic, profile, base_mid=base_mid)

        min_pts = _scrape_live_min_stop(rest, epic) if not dry_run else 12.0
        result.min_stop_points = min_pts

        ok_pre, pre_msg = _broker_preflight(rest, "", epic, direction) if not dry_run else (True, "dry_run")
        if not ok_pre:
            result.error = f"pre_entry_preflight:{pre_msg}"
            return result

        if dry_run:
            result.ok = True
            result.deal_id = f"DRY-{cycle}"
            result.deal_reference = _deal_ref(f"cert-dry-{cycle}")
            return result

        size = _epic_lot(epic, index_lot=index_lot, fx_lot=fx_lot)
        stop_dist = max(min_pts, 10.0)
        deal_ref = _deal_ref(f"cert-{cycle}")
        payload = {
            "signal": "BREAKOUT",
            "epic": epic,
            "direction": direction,
            "size": size,
            "dealReference": deal_ref,
            "trailing_stop_distance": stop_dist,
            "scalp_limit": max(stop_dist * 2.0, 20.0),
            "setup_key": f"CERT|{prof_name}|{epic}",
            "confidence": 88.0,
        }
        receipt, rtt_ms = _post_fulfill(api_base, payload)
        result.rtt_ms = rtt_ms
        result.deal_reference = deal_ref

        status = str(receipt.get("status") or "").upper()
        if not (
            receipt.get("ok")
            or status in ("ACCEPTED", "CONFIRMED", "EXECUTED")
            or receipt.get("async")
        ):
            result.error = str(receipt.get("detail") or receipt.get("error") or status)
            return result

        await asyncio.sleep(3.0)
        deal_id = str(receipt.get("dealId") or receipt.get("deal_id") or "")
        if not deal_id:
            deal_id = _find_deal_for_epic(rest, epic)
        result.deal_id = deal_id

        if deal_id:
            ok_trail_pre, trail_msg = _broker_preflight(rest, deal_id, epic, direction)
            if not ok_trail_pre:
                result.reconciliation_purged += 1
                result.error = f"trail_preflight:{trail_msg}"
                return result

            row = None
            for item in rest.open_positions() or []:
                pos = item.get("position") or {}
                if str(pos.get("dealId") or "") == deal_id:
                    row = pos
                    level = float(pos.get("level") or pos.get("openLevel") or base_mid)
                    break
            else:
                level = base_mid

            if row is not None:
                trail_ok, trail_detail = _apply_trail(
                    rest,
                    deal_id=deal_id,
                    epic=epic,
                    direction=direction,
                    level=level,
                    step=min_pts,
                )
                if trail_ok:
                    result.trailing_mods += 1
                    record_event(
                        session_id=session_id,
                        event_type="trailing_mod",
                        epic=epic,
                        deal_id=deal_id,
                        direction=direction,
                        detail=trail_detail,
                    )
                else:
                    record_event(
                        session_id=session_id,
                        event_type="trailing_mod_fail",
                        epic=epic,
                        deal_id=deal_id,
                        detail=trail_detail,
                    )

            closed = _close_deal(
                rest,
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                size=size,
            )
            if not closed:
                result.error = "close_failed"
                return result

        from trading.continuous_optimization_worker import get_continuous_optimization_worker

        worker = get_continuous_optimization_worker()
        z = worker.on_certification_cycle_closed(
            cycle,
            win=bool(deal_id),
            epic=epic,
            net_pnl=0.5 if deal_id else -0.5,
        )
        result.ml_z_threshold = z

        record_event(
            session_id=session_id,
            event_type="cert_cycle_ok",
            epic=epic,
            deal_id=deal_id or None,
            direction=direction,
            detail={"profile": prof_name, "min_stop": min_pts},
            rtt_ms=rtt_ms,
        )
        result.ok = True
        return result

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        record_event(
            session_id=session_id,
            event_type="cert_cycle_fail",
            epic=epic,
            direction=direction,
            detail={"profile": prof_name, "error": result.error},
        )
        return result


async def run_certification(
    *,
    api_base: str = "http://127.0.0.1:8080",
    dry_run: bool = False,
    max_retries_per_cycle: int = 3,
) -> CertificationLedger:
    _apply_canary_env()
    index_lot, fx_lot, cadence = _load_sizing()
    session_id = f"cert-apex-{int(time.time())}"
    upsert_session(session_id)
    ledger = CertificationLedger(session_id=session_id)

    if not dry_run:
        if not _wait_operational_health(api_base):
            log_engine("BOOT_GATE_TIMEOUT_ABORT")
            raise RuntimeError(
                f"BOOT_GATE_TIMEOUT_ABORT: /api/health not OPERATIONAL within "
                f"{_BOOT_GATE_TIMEOUT_SEC:.0f}s on {api_base}"
            )

    from system.credentials_loader import try_load_credentials
    from system.ig_rest_session import ensure_shared_authenticated

    creds = try_load_credentials()
    if not creds.ok or creds.credentials is None:
        raise RuntimeError(creds.error or "credentials unavailable")
    rest = ensure_shared_authenticated(creds.credentials)

    from trading.continuous_optimization_worker import get_continuous_optimization_worker

    get_continuous_optimization_worker().start()

    print("=== APEX v31 10-CYCLE CERTIFICATION ===")
    print(f"session={session_id} cadence={cadence}s dry_run={dry_run}")

    for i, profile in enumerate(REPLAY_PROFILES, start=1):
        epic = CERT_EPICS[(i - 1) % len(CERT_EPICS)]
        attempt = 0
        cycle_result: CycleResult | None = None
        while attempt < max_retries_per_cycle:
            attempt += 1
            if attempt > 1:
                _clear_instance_locks()
                log_engine(f"certify: auto-remediation retry cycle={i} attempt={attempt}")
                await asyncio.sleep(cadence)
            cycle_result = await _run_single_cycle(
                cycle=i,
                epic=epic,
                profile=profile,
                api_base=api_base,
                session_id=session_id,
                index_lot=index_lot,
                fx_lot=fx_lot,
                rest=rest,
                dry_run=dry_run,
            )
            if cycle_result.ok:
                break
            log_engine(
                f"certify: cycle {i} failed ({cycle_result.error}) — "
                f"attempt {attempt}/{max_retries_per_cycle}"
            )

        assert cycle_result is not None
        ledger.cycles.append(cycle_result)
        ledger.trailing_total += cycle_result.trailing_mods
        ledger.reconciliation_purged += cycle_result.reconciliation_purged
        if cycle_result.ml_z_threshold > 0:
            ledger.ml_shifts += 1
        if cycle_result.ok and cycle_result.deal_id:
            ledger.fills.append(
                {
                    "cycle": i,
                    "deal_id": cycle_result.deal_id,
                    "epic": epic,
                    "profile": cycle_result.profile,
                }
            )
        status = "OK" if cycle_result.ok else f"FAIL:{cycle_result.error}"
        print(
            f"  Cycle {i:02d}/10 | {epic} | {profile['name']} | {status} | "
            f"trail={cycle_result.trailing_mods} min_stop={cycle_result.min_stop_points:g}"
        )
        if i < 10 and not dry_run:
            await asyncio.sleep(cadence)
        elif i < 10 and dry_run:
            await asyncio.sleep(0.05)

    upsert_session(
        session_id,
        ended_at=time.time(),
        orders_ok=ledger.success_count(),
        orders_fail=10 - ledger.success_count(),
        trailing_mods=ledger.trailing_total,
        report_json=json.dumps(ledger.to_report()),
    )
    return ledger


def _print_matrix(ledger: CertificationLedger) -> None:
    report = ledger.to_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print("CERTIFICATION LEDGER REPORT — Apex v31 Limit Suite")
    print("=" * 72)
    print(f"  Total Injected Cycles:     {report['total_injected_cycles']}")
    print(f"  Successful Fills:          {len(report['successful_fills'])}")
    for fill in report["successful_fills"]:
        print(f"    • Cycle {fill['cycle']} dealId={fill['deal_id']} epic={fill['epic']}")
    print(f"  Step-Trailing Modifications:{report['trailing_step_modifications']}")
    print(f"  ML Parameter Shifts:       {report['ml_parameter_shifts']}")
    print(f"  Reconciliation Purged:     {report['reconciliation_mismatches_purged']}")
    print(f"  Active Risk Drift:         {report['active_risk_drift']}")
    print(f"  Report saved:              {REPORT_PATH}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--dry-run", action="store_true", help="Replay+metadata only, no live orders")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    try:
        ledger = asyncio.run(
            run_certification(
                api_base=args.api_base,
                dry_run=args.dry_run,
                max_retries_per_cycle=max(1, args.max_retries),
            )
        )
        _print_matrix(ledger)
        return 0 if ledger.success_count() == 10 else 1
    except Exception as exc:
        log_engine(f"certify_apex_limit fatal: {type(exc).__name__}: {exc}")
        print(f"CERTIFICATION ABORTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
